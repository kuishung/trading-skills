"""chart_drawings: per-user chart drawings, one JSON row per (user, symbol)

Moves the drawing overlay off browser localStorage and onto the server, so a
member's shapes follow them to any PC / browser instead of being stranded on
the machine that drew them.

Revision ID: f3a4b5c6d7e8
Revises: e4f5a6b7c8d9
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f3a4b5c6d7e8"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chart_drawings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("shapes", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", name="uq_chart_drawing_user_symbol"),
    )
    op.create_index("ix_chart_drawings_user_id", "chart_drawings", ["user_id"])
    op.create_index("ix_chart_drawings_symbol", "chart_drawings", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_chart_drawings_symbol", table_name="chart_drawings")
    op.drop_index("ix_chart_drawings_user_id", table_name="chart_drawings")
    op.drop_table("chart_drawings")
