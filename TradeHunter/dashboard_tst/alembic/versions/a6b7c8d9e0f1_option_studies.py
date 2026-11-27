"""option_studies: an option setup shared from the Option Finder, with its chart snapshot and discussion (v4.279)

The user (2026-11-27): *"in option finder, i want a right click on the tiles or the table item it will give the
moderator and above to be able to send the studies to telegram and discord the setup ... a snapshot of the
chart showing its Entry, SL, PT and also the detail of the selected strike so that user reading the telegram
and discord know the setup and can discuss on it."* Portable types only; the figures as JSON.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-11-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "option_studies" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "option_studies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("expiry", sa.String(10), nullable=False),
        sa.Column("right", sa.String(1), nullable=False, server_default="C"),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("entry", sa.Float(), nullable=True),
        sa.Column("entry_src", sa.String(10), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("worst", sa.Float(), nullable=True),
        sa.Column("mode", sa.String(10), nullable=True),
        sa.Column("valued_on", sa.String(10), nullable=True),
        sa.Column("figures", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("snapshot", sa.String(200), nullable=True),
        sa.Column("discussion_id", sa.Integer(), sa.ForeignKey("discussions.id"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_option_studies_symbol", "option_studies", ["symbol"])
    op.create_index("ix_option_studies_discussion_id", "option_studies", ["discussion_id"])


def downgrade() -> None:
    if "option_studies" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("option_studies")
