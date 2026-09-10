"""portfolio: per-trade exit lines + daily spread_checks history

Adds the storage behind the Portfolio page's daily monitor:

* ``option_spreads.roll_delta`` / ``.loss_stop_pct`` — per-trade overrides of the
  member's own exit lines (NULL = fall back to their default, then the playbook).
* ``spread_checks`` — one monitoring snapshot per spread per day, so the page can
  show a delta/loss SERIES rather than only today's number.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    """The Hermes database predates Alembic and was built by ``create_all``, so a
    column this migration adds may already exist there. Checking keeps the upgrade
    idempotent on that box without a hand-run stamp."""
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    return column in cols


def upgrade() -> None:
    if not _has_column("option_spreads", "roll_delta"):
        op.add_column("option_spreads", sa.Column("roll_delta", sa.Float(), nullable=True))
    if not _has_column("option_spreads", "loss_stop_pct"):
        op.add_column("option_spreads", sa.Column("loss_stop_pct", sa.Float(), nullable=True))

    bind = op.get_bind()
    if "spread_checks" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "spread_checks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("spread_id", sa.Integer(),
                      sa.ForeignKey("option_spreads.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("checked_on", sa.String(length=10), nullable=False),
            sa.Column("spot", sa.Float(), nullable=True),
            sa.Column("short_delta", sa.Float(), nullable=True),
            sa.Column("short_iv", sa.Float(), nullable=True),
            sa.Column("mark", sa.Float(), nullable=True),
            sa.Column("pl", sa.Float(), nullable=True),
            sa.Column("loss_pct", sa.Float(), nullable=True),
            sa.Column("dte", sa.Integer(), nullable=True),
            sa.Column("state", sa.String(length=10), nullable=False,
                      server_default="UNKNOWN"),
            sa.Column("action", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=12), nullable=False,
                      server_default="cboe"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("spread_id", "checked_on", name="uq_spread_check_day"),
        )
        op.create_index("ix_spread_checks_spread_id", "spread_checks", ["spread_id"])
        op.create_index("ix_spread_checks_checked_on", "spread_checks", ["checked_on"])


def downgrade() -> None:
    bind = op.get_bind()
    if "spread_checks" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_spread_checks_checked_on", table_name="spread_checks")
        op.drop_index("ix_spread_checks_spread_id", table_name="spread_checks")
        op.drop_table("spread_checks")
    if _has_column("option_spreads", "loss_stop_pct"):
        op.drop_column("option_spreads", "loss_stop_pct")
    if _has_column("option_spreads", "roll_delta"):
        op.drop_column("option_spreads", "roll_delta")
