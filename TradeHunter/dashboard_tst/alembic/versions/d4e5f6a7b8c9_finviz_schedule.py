"""finviz_filters: per-filter run schedule

Adds run_interval / last_run_at / next_run_at so the agent's poll cron runs
each filter on its own cadence (off|daily|weekly|monthly|quarterly).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("finviz_filters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("run_interval", sa.String(length=12), nullable=False, server_default="off")
        )
        batch_op.add_column(sa.Column("last_run_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("next_run_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_finviz_filters_next_run_at"), ["next_run_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("finviz_filters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_finviz_filters_next_run_at"))
        batch_op.drop_column("next_run_at")
        batch_op.drop_column("last_run_at")
        batch_op.drop_column("run_interval")
