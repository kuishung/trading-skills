"""user_watchlist: per-user "My Watchlist"

Creates the user_watchlist table -- one row per (user, starred symbol). The
ticker's market data stays in matp_levels (shared); this table only records
whose personal list a symbol is on.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_watchlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    )
    op.create_index("ix_user_watchlist_user_id", "user_watchlist", ["user_id"])
    op.create_index("ix_user_watchlist_symbol", "user_watchlist", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_user_watchlist_symbol", table_name="user_watchlist")
    op.drop_index("ix_user_watchlist_user_id", table_name="user_watchlist")
    op.drop_table("user_watchlist")
