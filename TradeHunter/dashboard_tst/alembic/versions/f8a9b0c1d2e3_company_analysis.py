"""company_analysis: per-ticker dossier sections

Creates the company_analysis table (one row per symbol+section) backing the
Company Analysis page. See dashboard_tst/COMPANY_ANALYSIS_DESIGN.md.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_analysis",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("industry", sa.String(length=80), nullable=True),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "section", name="uq_company_analysis_symbol_section"),
    )
    op.create_index("ix_company_analysis_symbol", "company_analysis", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_company_analysis_symbol", table_name="company_analysis")
    op.drop_table("company_analysis")
