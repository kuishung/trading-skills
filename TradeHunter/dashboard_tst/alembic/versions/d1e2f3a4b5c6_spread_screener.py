"""options: bull put spread screener - iv_history, spread_scans, spread_candidates

* ``iv_history`` - one 30-day IV reading per symbol per day (percent), the series
  behind the screener's IV-percentile column. Filed nightly from Cboe, seedable
  from IB Gateway (deploy/iv_seed_ibkr.py).
* ``spread_scans`` - one row per nightly run (freshness pill).
* ``spread_candidates`` - every bull put spread the scan found, one column per
  Barchart screen field, replaced per scan day.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if "iv_history" not in have:
        op.create_table(
            "iv_history",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("on", sa.String(length=10), nullable=False),
            sa.Column("iv30", sa.Float(), nullable=False),
            sa.Column("spot", sa.Float(), nullable=True),
            sa.Column("source", sa.String(length=12), nullable=False, server_default="cboe"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("symbol", "on", name="uq_iv_history_day"),
        )
        op.create_index("ix_iv_history_symbol", "iv_history", ["symbol"])
        op.create_index("ix_iv_history_on", "iv_history", ["on"])

    if "spread_scans" not in have:
        op.create_table(
            "spread_scans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scan_on", sa.String(length=10), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("symbols", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("priced", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("note", sa.Text(), nullable=True),
        )
        op.create_index("ix_spread_scans_scan_on", "spread_scans", ["scan_on"])

    if "spread_candidates" not in have:
        op.create_table(
            "spread_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scan_on", sa.String(length=10), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("spot", sa.Float(), nullable=False),
            sa.Column("expiry", sa.String(length=10), nullable=False),
            sa.Column("dte", sa.Integer(), nullable=False),
            sa.Column("monthly", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("iv30", sa.Float(), nullable=True),
            sa.Column("iv_pct", sa.Float(), nullable=True),
            sa.Column("iv_n", sa.Integer(), nullable=True),
            sa.Column("short_strike", sa.Float(), nullable=False),
            sa.Column("long_strike", sa.Float(), nullable=False),
            sa.Column("width", sa.Float(), nullable=False),
            sa.Column("moneyness", sa.Float(), nullable=False),
            sa.Column("short_bid", sa.Float(), nullable=True),
            sa.Column("short_ask", sa.Float(), nullable=True),
            sa.Column("long_bid", sa.Float(), nullable=True),
            sa.Column("long_ask", sa.Float(), nullable=True),
            sa.Column("credit", sa.Float(), nullable=True),
            sa.Column("credit_mid", sa.Float(), nullable=True),
            sa.Column("credit_pct", sa.Float(), nullable=True),
            sa.Column("max_loss", sa.Float(), nullable=True),
            sa.Column("short_delta", sa.Float(), nullable=True),
            sa.Column("long_delta", sa.Float(), nullable=True),
            sa.Column("otm_prob", sa.Float(), nullable=True),
            sa.Column("short_iv", sa.Float(), nullable=True),
            sa.Column("short_vol", sa.Integer(), nullable=True),
            sa.Column("short_oi", sa.Integer(), nullable=True),
            sa.Column("long_vol", sa.Integer(), nullable=True),
            sa.Column("long_oi", sa.Integer(), nullable=True),
            sa.Column("earnings", sa.String(length=10), nullable=True),
            sa.Column("earnings_before_expiry", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_spread_candidates_scan_on", "spread_candidates", ["scan_on"])
        op.create_index("ix_spread_cand_scan_symbol", "spread_candidates",
                        ["scan_on", "symbol"])


def downgrade() -> None:
    have = _tables()
    if "spread_candidates" in have:
        op.drop_index("ix_spread_cand_scan_symbol", table_name="spread_candidates")
        op.drop_index("ix_spread_candidates_scan_on", table_name="spread_candidates")
        op.drop_table("spread_candidates")
    if "spread_scans" in have:
        op.drop_index("ix_spread_scans_scan_on", table_name="spread_scans")
        op.drop_table("spread_scans")
    if "iv_history" in have:
        op.drop_index("ix_iv_history_on", table_name="iv_history")
        op.drop_index("ix_iv_history_symbol", table_name="iv_history")
        op.drop_table("iv_history")
