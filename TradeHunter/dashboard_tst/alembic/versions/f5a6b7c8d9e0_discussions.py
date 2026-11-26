"""discussions + contributions: one thread per subject on Discord / Telegram, read back (v4.275)

The user (2026-11-26): *"we moderator put up a topic or study in the chat either discord or telegram, there
will be a thread created for that topic or studies and user can contribute in the thread which can be read in
tradehunter"* - and: build the structure first; which subjects open a discussion is not decided yet.
DISCUSSIONS_DESIGN.md is the contract. Portable types only; JSON for the two small maps.

Revision ID: f5a6b7c8d9e0
Revises: c5d8e2a4f7b1
Create Date: 2026-11-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f5a6b7c8d9e0"
down_revision = "c5d8e2a4f7b1"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if "discussions" not in have:
        op.create_table(
            "discussions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("ref", sa.String(80), nullable=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.Column("discord_channel_id", sa.String(40), nullable=True),
            sa.Column("discord_thread_id", sa.String(40), nullable=True),
            sa.Column("telegram_chat_id", sa.String(40), nullable=True),
            sa.Column("telegram_topic_id", sa.String(40), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("n_contributions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("channel_errors", sa.JSON(), nullable=True),
        )
        op.create_index("ix_discussions_kind", "discussions", ["kind"])
        op.create_index("ix_discussions_ref", "discussions", ["ref"])
        op.create_index("ix_discussions_discord_thread_id", "discussions", ["discord_thread_id"])
        op.create_index("ix_discussions_telegram_topic_id", "discussions", ["telegram_topic_id"])
    if "contributions" not in have:
        op.create_table(
            "contributions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("discussion_id", sa.Integer(), sa.ForeignKey("discussions.id"), nullable=False),
            sa.Column("channel", sa.String(10), nullable=False),
            sa.Column("external_id", sa.String(60), nullable=True),
            sa.Column("author_external_id", sa.String(60), nullable=True),
            sa.Column("author_name", sa.String(120), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("posted_at", sa.DateTime(), nullable=False),
            sa.Column("reply_to_external_id", sa.String(60), nullable=True),
            sa.Column("is_opener", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("mirrored", sa.JSON(), nullable=True),
            sa.UniqueConstraint("discussion_id", "channel", "external_id", name="uq_contribution_message"),
        )
        op.create_index("ix_contributions_discussion_id", "contributions", ["discussion_id"])


def downgrade() -> None:
    have = _tables()
    if "contributions" in have:
        op.drop_table("contributions")
    if "discussions" in have:
        op.drop_table("discussions")
