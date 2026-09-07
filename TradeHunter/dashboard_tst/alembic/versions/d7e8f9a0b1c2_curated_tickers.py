"""curated_tickers: per-member dated trade plans (entry / stop / target)

Replaces the Portfolio placeholder with something that holds real user data. The
table stores only the DECISION -- ticker, the date it was called, and the three
levels. Whether it triggered and what it made is recomputed from daily bars on
every read, so no outcome column can go stale or disagree with the prices.

No unique constraint on (user, symbol): the same ticker can honestly be curated
more than once, at different dates and different levels, and each of those is a
separate call to be judged on its own.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "curated_tickers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("curated_on", sa.String(length=10), nullable=False),
        sa.Column("entry", sa.Float(), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_curated_tickers_user_id", "curated_tickers", ["user_id"])
    op.create_index("ix_curated_tickers_symbol", "curated_tickers", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_curated_tickers_symbol", table_name="curated_tickers")
    op.drop_index("ix_curated_tickers_user_id", table_name="curated_tickers")
    op.drop_table("curated_tickers")
