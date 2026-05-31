"""agent_heartbeats: liveness + cron self-report from the Nous Hermes agent

The agent is outbound-only; it POSTs a heartbeat on each poll (version + raw
`crontab -l` + its clock). One row per agent name (upserted in the route).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=60), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=True),
        sa.Column("crons", sa.Text(), nullable=True),
        sa.Column("host", sa.String(length=120), nullable=True),
        sa.Column("polled_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_heartbeats_agent"), "agent_heartbeats", ["agent"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_heartbeats_agent"), table_name="agent_heartbeats")
    op.drop_table("agent_heartbeats")
