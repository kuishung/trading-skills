"""agent_heartbeats: measured health (agent_ok / gateway / disk)

The heartbeat proved only that the Linux box was powered on -- the agent was
dead for ten days (2026-08-06 to 2026-08-16) while the beat kept landing and the
/agent pill stayed green. heartbeat.sh now measures whether the agent can
actually run (`hermes --version`), the gateway unit state, and root disk usage;
this column stores that object.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_heartbeats") as batch:
        batch.add_column(sa.Column("health", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_heartbeats") as batch:
        batch.drop_column("health")
