"""rrg_points: the chart's real RRG coordinates, replacing the approximation

The panel computed its own JdK approximation, which cannot be relied on to agree
with the licensed indicator: sectors near a 100 line landed in the wrong quadrant
(Energy read Weakening while the chart said Improving). This table holds the
chart's own numbers when we have them; the approximation stays as the fallback.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rrg_points",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("as_of", sa.String(length=10), nullable=False),
        sa.Column("rs_ratio", sa.Float(), nullable=False),
        sa.Column("rs_momentum", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "timeframe", name="uq_rrg_point_symbol_tf"),
    )
    op.create_index("ix_rrg_points_symbol", "rrg_points", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_rrg_points_symbol", table_name="rrg_points")
    op.drop_table("rrg_points")
