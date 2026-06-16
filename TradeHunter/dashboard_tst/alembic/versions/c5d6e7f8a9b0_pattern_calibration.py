"""patterns: calibration state (fitted thresholds + validation pass-rate)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patterns", sa.Column("detector_thresholds", sa.JSON(), nullable=True))
    op.add_column("patterns", sa.Column("detector_version", sa.String(length=20), nullable=True))
    op.add_column("patterns", sa.Column("calib_pass_rate", sa.Float(), nullable=True))
    op.add_column("patterns", sa.Column("calib_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("patterns", "calib_at")
    op.drop_column("patterns", "calib_pass_rate")
    op.drop_column("patterns", "detector_version")
    op.drop_column("patterns", "detector_thresholds")
