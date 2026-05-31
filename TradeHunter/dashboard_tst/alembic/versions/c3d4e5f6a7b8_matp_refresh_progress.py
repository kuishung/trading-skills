"""matp_refresh_requests: live progress counters

Adds progress_done / progress_total so the runs panel can show a real progress
bar as the agent works the universe.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("matp_refresh_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("progress_done", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("progress_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("matp_refresh_requests", schema=None) as batch_op:
        batch_op.drop_column("progress_total")
        batch_op.drop_column("progress_done")
