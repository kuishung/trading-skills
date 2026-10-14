"""scr_earnings + scr_earnings_day: the earnings dates the collector has seen

v4.143 (the Volatility surface's hover card marks earnings on a contract's IV history): the
collector reads Nasdaq's earnings calendar daily for the next 70 days (``scr_underlying.
earnings_date`` keeps only the NEXT date per stock); from now on every (symbol, date) it sees
is kept in ``scr_earnings``, and a one-off backfill reads the past year's calendar days so the
history is complete at once. ``scr_earnings_day`` records which calendar days were fetched
(the backfill's work list; a past day is never fetched twice).

Mirrors ``app/screener_models.ScrEarnings`` / ``ScrEarningsDay``. Portable types only.

Revision ID: d4e6f8a0b2c4
Revises: c3d9e5f1a7b2
Create Date: 2026-10-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d4e6f8a0b2c4"
down_revision = "c3d9e5f1a7b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scr_earnings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("on", sa.String(length=10), nullable=False),
        sa.Column("when_", sa.String(length=4), nullable=True),
        sa.Column("src", sa.String(length=8), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "on", name="uq_scr_earnings"),
    )
    op.create_index("ix_scr_earnings_symbol", "scr_earnings", ["symbol"])
    op.create_table(
        "scr_earnings_day",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("on", sa.String(length=10), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("n_rows", sa.Integer(), nullable=True),
        sa.UniqueConstraint("on", name="uq_scr_earnings_day"),
    )


def downgrade() -> None:
    op.drop_table("scr_earnings_day")
    op.drop_index("ix_scr_earnings_symbol", table_name="scr_earnings")
    op.drop_table("scr_earnings")
