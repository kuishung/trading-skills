"""matp_refresh_requests: collaborator-triggered ad-hoc refresh queue

The agent polls pending rows, runs the MATP work, pushes via /api/matp, and
marks each row done/failed. Moderators/admins enqueue (route-enforced).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "matp_refresh_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("filter_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["filter_id"], ["finviz_filters.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("matp_refresh_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_matp_refresh_requests_symbol"), ["symbol"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_matp_refresh_requests_status"), ["status"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("matp_refresh_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_matp_refresh_requests_status"))
        batch_op.drop_index(batch_op.f("ix_matp_refresh_requests_symbol"))
    op.drop_table("matp_refresh_requests")
