"""option_calc_requests.iv_shift: the IV change on the target date of a saved setup (v4.217)

The user (2026-10-25): *"Start with the IV-change scenario on the target date in the calculator."* - vol
points added to every leg's implied volatility on the target date only (today's prices keep today's IV);
NULL / 0 = the workbook's own assumption, IV held constant. Manual setups only. Portable types only.

Revision ID: c5d8e2a4f7b1
Revises: b9d2e4f6a8c1
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c5d8e2a4f7b1"
down_revision = "b9d2e4f6a8c1"
branch_labels = None
depends_on = None

TABLE = "option_calc_requests"
COLUMN = "iv_shift"


def _columns() -> set[str]:
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    cols = _columns()
    if cols and COLUMN not in cols:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    if COLUMN in _columns():
        with op.batch_alter_table(TABLE) as b:
            b.drop_column(COLUMN)
