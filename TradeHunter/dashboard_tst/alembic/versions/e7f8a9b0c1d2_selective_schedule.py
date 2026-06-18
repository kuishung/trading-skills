"""selective_schedule: singleton refresh cadence for the ad-hoc 'Selective tickers'

The selective set = active MATPLevel rows with filter_id NULL (names keyed in by
hand). This one-row table gives them their OWN scheduled MATP refresh, independent
of any Finviz filter being due. The agent's poll reads it via /api/due-filters and
advances it on a completed selective run.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "selective_schedule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_interval", sa.String(length=12), nullable=False, server_default="off"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_selective_schedule_next_run_at"), "selective_schedule", ["next_run_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_selective_schedule_next_run_at"), table_name="selective_schedule")
    op.drop_table("selective_schedule")
