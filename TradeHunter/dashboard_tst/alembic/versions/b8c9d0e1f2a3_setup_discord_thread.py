"""setups: per-study Discord thread id

Stores the bot-created Discord thread id for a study so the study page can read
and show that thread's discussion (webhooks are write-only; reading needs a bot).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("setups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("discord_thread_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("setups", schema=None) as batch_op:
        batch_op.drop_column("discord_thread_id")
