"""ingest_health: pushed parquet-ingest health report

Stores the latest parquet-ingest freshness/log report POSTed by the Hermes-side
reporter cron (POST /api/ingest/health), shown on the Data Ingest page.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(length=120), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ingest_health_host", "ingest_health", ["host"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_ingest_health_host", table_name="ingest_health")
    op.drop_table("ingest_health")
