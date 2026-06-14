"""research topics, messages, runs

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-06-13

The Research feature: a member co-designs a research topic with the LLM
(DeepSeek, called directly by the dashboard for the planning chat), records the
agreed plan as Markdown, schedules it on a per-topic cron, and the Nous agent
runs it outbound-only. Portable column types (SQLite dev -> Postgres prod).
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_topics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="company"),
        sa.Column("subject", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="draft"),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("plan_md", sa.Text(), nullable=True),
        sa.Column("schedule_cron", sa.String(length=120), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_topics_owner_id", "research_topics", ["owner_id"])

    op.create_table(
        "research_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["topic_id"], ["research_topics.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_messages_topic_id", "research_messages", ["topic_id"])

    op.create_table(
        "research_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(length=12), nullable=False, server_default="manual"),
        sa.Column("queued_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("output_md_path", sa.String(length=512), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["topic_id"], ["research_topics.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_runs_topic_id", "research_runs", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_research_runs_topic_id", table_name="research_runs")
    op.drop_table("research_runs")
    op.drop_index("ix_research_messages_topic_id", table_name="research_messages")
    op.drop_table("research_messages")
    op.drop_index("ix_research_topics_owner_id", table_name="research_topics")
    op.drop_table("research_topics")
