"""option calculator: option_calc_requests + option_calc_results

OPTION_CALCULATOR_DESIGN.md steps 2-3. A request is a member's saved setup (the two
prices, a horizon, optional legs / criteria); a result is what the Hermes lane computed
for it on one Massive pass (resolved legs, the engine's figures, the payoff curves).
Both portable types only (Integer / Float / String / Text / Boolean / JSON / DateTime).

Revision ID: a7c3e91d5b24
Revises: 3069a57385d1
Create Date: 2026-10-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a7c3e91d5b24"
down_revision = "3069a57385d1"
branch_labels = None
depends_on = None

REQ = "option_calc_requests"
RES = "option_calc_results"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if REQ not in have:
        op.create_table(
            REQ,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("kind", sa.String(length=8), nullable=False, server_default="manual"),
            sa.Column("name", sa.String(length=80), nullable=True),
            sa.Column("target", sa.Float(), nullable=False),
            sa.Column("worst", sa.Float(), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=True),
            sa.Column("sets", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("legs", sa.JSON(), nullable=True),
            sa.Column("criteria", sa.JSON(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_pass_id", sa.Integer(), nullable=True),
            sa.Column("last_computed_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_option_calc_requests_symbol", REQ, ["symbol"])
        op.create_index("ix_option_calc_requests_user_active", REQ, ["user_id", "active"])
    if RES not in have:
        op.create_table(
            RES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.Integer(), sa.ForeignKey(REQ + ".id", ondelete="CASCADE"), nullable=False),
            sa.Column("pass_id", sa.Integer(), nullable=True),
            sa.Column("session", sa.String(length=10), nullable=True),
            sa.Column("as_of", sa.DateTime(), nullable=True),
            sa.Column("spot", sa.Float(), nullable=True),
            sa.Column("family", sa.String(length=24), nullable=False, server_default="manual"),
            sa.Column("rank", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("horizon_days", sa.Integer(), nullable=True),
            sa.Column("target_date", sa.String(length=10), nullable=True),
            sa.Column("legs", sa.JSON(), nullable=False),
            sa.Column("figures", sa.JSON(), nullable=False),
            sa.Column("curves", sa.JSON(), nullable=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_option_calc_results_req_pass", RES, ["request_id", "pass_id"])
        op.create_index("ix_option_calc_results_req_created", RES, ["request_id", "created_at"])


def downgrade() -> None:
    have = _tables()
    if RES in have:
        op.drop_index("ix_option_calc_results_req_created", table_name=RES)
        op.drop_index("ix_option_calc_results_req_pass", table_name=RES)
        op.drop_table(RES)
    if REQ in have:
        op.drop_index("ix_option_calc_requests_user_active", table_name=REQ)
        op.drop_index("ix_option_calc_requests_symbol", table_name=REQ)
        op.drop_table(REQ)
