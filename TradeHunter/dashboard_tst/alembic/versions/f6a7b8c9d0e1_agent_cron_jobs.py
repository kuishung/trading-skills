"""agent_heartbeats: structured cron_jobs (full prompt per cron)

Adds a JSON column holding [{id,schedule,skills,prompt,next_run,active}] so the
/agent page can show what each cron actually does instead of the truncated Name.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_heartbeats", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cron_jobs", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_heartbeats", schema=None) as batch_op:
        batch_op.drop_column("cron_jobs")
