"""macro indicators + readings (the tracked-series layer)

Phase A of MACRO_STUDY_DESIGN.md: named indicators per macro topic, and their
observations over time, so each topic shows a TREND rather than a snapshot.
Definitions live in the DB so the indicator set can be revised without a deploy.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "macro_indicators",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("source_ref", sa.String(length=60), nullable=True),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("transform", sa.String(length=10), nullable=False, server_default="level"),
        sa.Column("higher_is", sa.String(length=12), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_macro_indicator_key"),
    )
    op.create_index("ix_macro_indicators_key", "macro_indicators", ["key"])
    op.create_index("ix_macro_indicators_section", "macro_indicators", ["section"])

    op.create_table(
        "macro_readings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("indicator_key", sa.String(length=40), nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("vintage", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("indicator_key", "as_of", name="uq_macro_reading_key_asof"),
    )
    op.create_index("ix_macro_readings_indicator_key", "macro_readings", ["indicator_key"])
    op.create_index("ix_macro_readings_as_of", "macro_readings", ["as_of"])


def downgrade() -> None:
    op.drop_index("ix_macro_readings_as_of", table_name="macro_readings")
    op.drop_index("ix_macro_readings_indicator_key", table_name="macro_readings")
    op.drop_table("macro_readings")
    op.drop_index("ix_macro_indicators_section", table_name="macro_indicators")
    op.drop_index("ix_macro_indicators_key", table_name="macro_indicators")
    op.drop_table("macro_indicators")
