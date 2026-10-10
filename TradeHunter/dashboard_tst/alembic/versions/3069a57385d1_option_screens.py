"""options screener: option_screens - a member's saved screeners

One row per (member, screen, name): the Options Screener payload (filters, sort, view,
flag earnings - OPTIONS_SCREENER_DESIGN.md §5 / §7) the member saved under a name. At
most one row per (member, screen) is the default that loads when the screen opens
(kept by the route, not a constraint, so the swap is portable).

Revision ID: 3069a57385d1
Revises: 7c1e5a9d2b40
Create Date: 2026-10-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "3069a57385d1"
down_revision = "7c1e5a9d2b40"
branch_labels = None
depends_on = None

TABLE = "option_screens"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("screen_key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "screen_key", "name", name="uq_option_screens_user_screen_name"),
    )
    op.create_index("ix_option_screens_user_screen", TABLE, ["user_id", "screen_key"])


def downgrade() -> None:
    if TABLE in _tables():
        op.drop_index("ix_option_screens_user_screen", table_name=TABLE)
        op.drop_table(TABLE)
