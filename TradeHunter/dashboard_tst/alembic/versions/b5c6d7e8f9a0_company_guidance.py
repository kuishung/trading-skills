"""company_guidance: forward guidance figures extracted from 8-K earnings releases

Guidance is not in XBRL and not in the 10-Q corpus -- it is prose in the 8-K
item-2.02 press release -- so it needs its own store. Every row keeps the verbatim
sentence and the exhibit URL so each number is auditable to its source.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_guidance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("filed", sa.String(length=10), nullable=True),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("basis", sa.String(length=20), nullable=True),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("mid", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("sentence", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("accession", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "period", "metric", "basis",
                            name="uq_company_guidance_row"),
    )
    op.create_index("ix_company_guidance_symbol", "company_guidance", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_company_guidance_symbol", table_name="company_guidance")
    op.drop_table("company_guidance")
