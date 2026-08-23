"""option_spreads: tracked vertical spreads for the management-rule monitor

Tracking only — nothing in this table places or modifies an order.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa


revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "option_spreads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("strategy", sa.String(length=24), nullable=False),
        sa.Column("expiry", sa.String(length=10), nullable=False),
        sa.Column("short_strike", sa.Float(), nullable=False),
        sa.Column("long_strike", sa.Float(), nullable=False),
        sa.Column("credit", sa.Float(), nullable=True),
        sa.Column("contracts", sa.Integer(), nullable=False),
        sa.Column("entry_delta", sa.Float(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_option_spreads_user_id", "option_spreads", ["user_id"])
    op.create_index("ix_option_spreads_symbol", "option_spreads", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_option_spreads_symbol", table_name="option_spreads")
    op.drop_index("ix_option_spreads_user_id", table_name="option_spreads")
    op.drop_table("option_spreads")
