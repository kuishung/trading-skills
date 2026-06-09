"""edgar_ingest_health: pushed EDGAR earnings-filing ingest report

Stores the latest EDGAR-corpus health report POSTed by the AI-Hermes reporter
(POST /api/ingest/edgar). The EDGAR filings live on AI-Hermes (the Windows file
server) — the web app can't read them over the network, so AI-Hermes scans the
corpus + earnings dates and reports in, like the parquet ingest_health table.
Shown on the Data Ingest page.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edgar_ingest_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(length=120), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_edgar_ingest_health_host", "edgar_ingest_health", ["host"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_edgar_ingest_health_host", table_name="edgar_ingest_health")
    op.drop_table("edgar_ingest_health")
