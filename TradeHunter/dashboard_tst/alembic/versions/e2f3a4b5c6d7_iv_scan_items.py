"""options: iv_scan_items - the member's TWS High IV Rank scan as a watchlist

One row per (member, ticker): the scan order TWS returned, when it was scanned,
and the IV rank / percentile / current IV read from the member's TWS afterwards.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "iv_scan_items" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "iv_scan_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_at", sa.DateTime(), nullable=True),
        sa.Column("iv_rank", sa.Float(), nullable=True),
        sa.Column("iv_pct", sa.Float(), nullable=True),
        sa.Column("iv_current", sa.Float(), nullable=True),
        sa.Column("iv_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "symbol", name="uq_iv_scan_user_symbol"),
    )
    op.create_index("ix_iv_scan_items_user_id", "iv_scan_items", ["user_id"])
    op.create_index("ix_iv_scan_items_symbol", "iv_scan_items", ["symbol"])


def downgrade() -> None:
    if "iv_scan_items" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_index("ix_iv_scan_items_symbol", table_name="iv_scan_items")
        op.drop_index("ix_iv_scan_items_user_id", table_name="iv_scan_items")
        op.drop_table("iv_scan_items")
