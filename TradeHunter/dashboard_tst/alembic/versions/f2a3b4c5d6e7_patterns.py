"""pattern trainer: patterns + pattern_lessons

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patterns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=400), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="learning"),
        sa.Column("chart_symbol", sa.String(length=20), nullable=True),
        sa.Column("chart_timeframe", sa.String(length=8), nullable=True),
        sa.Column("pattern_md", sa.Text(), nullable=True),
        sa.Column("detect_py", sa.Text(), nullable=True),
        sa.Column("md_path", sa.String(length=512), nullable=True),
        sa.Column("script_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_patterns_owner_id", "patterns", ["owner_id"])
    op.create_index("ix_patterns_slug", "patterns", ["slug"])

    op.create_table(
        "pattern_lessons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pattern_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("marked", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["pattern_id"], ["patterns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pattern_lessons_pattern_id", "pattern_lessons", ["pattern_id"])


def downgrade() -> None:
    op.drop_index("ix_pattern_lessons_pattern_id", table_name="pattern_lessons")
    op.drop_table("pattern_lessons")
    op.drop_index("ix_patterns_slug", table_name="patterns")
    op.drop_index("ix_patterns_owner_id", table_name="patterns")
    op.drop_table("patterns")
