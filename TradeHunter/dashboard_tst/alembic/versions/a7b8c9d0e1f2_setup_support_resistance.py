"""setups: curator support / resistance levels

Adds support + resistance (horizontal levels the curator determines during a
study) to the setups table, alongside the existing entry / stop_loss /
profit_target. R:R is computed at display time, not stored.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("setups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("support", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("resistance", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("setups", schema=None) as batch_op:
        batch_op.drop_column("resistance")
        batch_op.drop_column("support")
