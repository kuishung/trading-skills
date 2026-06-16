"""pattern_examples: kind (polarity) + geometry (drawn trendlines)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pattern_examples",
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="positive"),
    )
    op.add_column("pattern_examples", sa.Column("geometry", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pattern_examples", "geometry")
    op.drop_column("pattern_examples", "kind")
