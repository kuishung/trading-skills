"""macro_analysis: written analysis per macro board section

Backs the /macro two-pane board (left rail = the six canonical macro topics,
right pane = that topic's analysis). One row per section; the computed parts
(cross-asset strip, breadth) are derived live and deliberately NOT stored.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "macro_analysis",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("section", name="uq_macro_analysis_section"),
    )
    op.create_index("ix_macro_analysis_section", "macro_analysis", ["section"])


def downgrade() -> None:
    op.drop_index("ix_macro_analysis_section", table_name="macro_analysis")
    op.drop_table("macro_analysis")
