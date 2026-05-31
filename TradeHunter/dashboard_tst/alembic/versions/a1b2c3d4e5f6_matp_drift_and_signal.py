"""matp_levels: universe-drift tracking + bounce signal + n_targets

Adds to matp_levels:
  - n_targets         post-earnings count behind the median (board badge)
  - status            'active' | 'dropped' (Finviz-screen membership)
  - filter_id         which saved Finviz filter sourced the ticker (FK)
  - last_seen_at      last run that included the ticker
  - signal*           EMA20/EMA50 bounce setup: bucket + entry/stop/target/rr

Revision ID: a1b2c3d4e5f6
Revises: d555dc88d20b
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "d555dc88d20b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("matp_levels", schema=None) as batch_op:
        batch_op.add_column(sa.Column("n_targets", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status", sa.String(length=12), nullable=False, server_default="active"
            )
        )
        batch_op.add_column(sa.Column("filter_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("signal", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("signal_entry", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("signal_stop", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("signal_target", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("signal_rr", sa.Float(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_matp_levels_filter_id"), ["filter_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_matp_levels_filter_id_finviz_filters",
            "finviz_filters",
            ["filter_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("matp_levels", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_matp_levels_filter_id_finviz_filters", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_matp_levels_filter_id"))
        batch_op.drop_column("signal_rr")
        batch_op.drop_column("signal_target")
        batch_op.drop_column("signal_stop")
        batch_op.drop_column("signal_entry")
        batch_op.drop_column("signal")
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("filter_id")
        batch_op.drop_column("status")
        batch_op.drop_column("n_targets")
