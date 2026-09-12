"""portfolio: per-leg entry, fuller daily greeks, profit-target + DTE-floor lines

* ``option_spreads``: ``short_price``/``long_price`` (the fills, per share),
  ``short_entry_delta``/``long_entry_delta``/``entry_iv`` (the chain at entry),
  ``profit_target_pct``/``dte_floor`` (two more per-trade exit-line overrides).
* ``spread_checks``: ``long_delta``, ``net_delta``, ``theta``, ``long_iv``,
  ``profit_pct`` — the position's full daily shape, not only the short delta.

All nullable, all additive; existing rows keep grading exactly as before.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None

_SPREAD_COLS = [
    ("profit_target_pct", sa.Float()),
    ("dte_floor", sa.Integer()),
    ("short_price", sa.Float()),
    ("long_price", sa.Float()),
    ("short_entry_delta", sa.Float()),
    ("long_entry_delta", sa.Float()),
    ("entry_iv", sa.Float()),
]
_CHECK_COLS = [
    ("long_delta", sa.Float()),
    ("net_delta", sa.Float()),
    ("theta", sa.Float()),
    ("long_iv", sa.Float()),
    ("profit_pct", sa.Float()),
]


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # Guarded like b9c0d1e2f3a4: the Hermes DB predates Alembic, so a column may
    # already exist there if create_all ever ran against a newer model.
    have = _cols("option_spreads")
    for name, typ in _SPREAD_COLS:
        if name not in have:
            op.add_column("option_spreads", sa.Column(name, typ, nullable=True))
    have = _cols("spread_checks")
    for name, typ in _CHECK_COLS:
        if name not in have:
            op.add_column("spread_checks", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    have = _cols("spread_checks")
    with op.batch_alter_table("spread_checks") as b:
        for name, _ in _CHECK_COLS:
            if name in have:
                b.drop_column(name)
    have = _cols("option_spreads")
    with op.batch_alter_table("option_spreads") as b:
        for name, _ in _SPREAD_COLS:
            if name in have:
                b.drop_column(name)
