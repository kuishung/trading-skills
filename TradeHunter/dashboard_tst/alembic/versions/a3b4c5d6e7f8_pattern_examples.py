"""pattern trainer: pattern_examples (saved teaching-example gallery)

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pattern_examples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pattern_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("start_t", sa.String(length=32), nullable=False),
        sa.Column("end_t", sa.String(length=32), nullable=False),
        sa.Column("n_bars", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["pattern_id"], ["patterns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pattern_examples_pattern_id", "pattern_examples", ["pattern_id"])


def downgrade() -> None:
    op.drop_index("ix_pattern_examples_pattern_id", table_name="pattern_examples")
    op.drop_table("pattern_examples")
