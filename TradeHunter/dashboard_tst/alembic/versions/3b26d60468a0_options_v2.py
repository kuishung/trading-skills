"""options v2: opt_quote, opt_underlying, opt_underlying_daily, opt_refresh_log,
opt_collector_status

The IBKR-only shared option data of OPTIONS_V2_DESIGN.md §2.2. Five new tables,
created in ORDER below and dropped in reverse; nothing existing is altered or
dropped (the v1 tables stay, unused). Each table is guarded with
``get_table_names()`` because ``app.db.init_db`` runs ``create_all`` on a legacy
DB before stamping, so a table may already exist when this revision runs (the
f4a5b6c7d8e9 pattern). Portable types only (SQLite dev, Postgres prod).

Revision ID: 3b26d60468a0
Revises: f4a5b6c7d8e9
Create Date: 2026-10-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "3b26d60468a0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None

ORDER = [
    "opt_quote", "opt_underlying", "opt_underlying_daily", "opt_refresh_log",
    "opt_collector_status",
]

# Non-unique indexes per table (name, columns); dropped before the table.
_INDEXES: dict[str, list[tuple[str, list[str]]]] = {
    "opt_quote": [
        ("ix_opt_quote_symbol_expiry", ["symbol", "expiry"]),
        ("ix_opt_quote_as_of", ["as_of"]),
    ],
    "opt_underlying": [],
    "opt_underlying_daily": [],
    "opt_refresh_log": [
        ("ix_opt_refresh_log_symbol_as_of", ["symbol", "as_of"]),
    ],
    "opt_collector_status": [],
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> None:
    for name, cols in _INDEXES[table]:
        op.create_index(name, table, cols)


def _create_opt_quote() -> None:
    op.create_table(
        "opt_quote",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("expiry", sa.String(length=10), nullable=False),
        sa.Column("right", sa.String(length=1), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("mid", sa.Float(), nullable=True),
        sa.Column("last", sa.Float(), nullable=True),
        sa.Column("bid_size", sa.Integer(), nullable=True),
        sa.Column("ask_size", sa.Integer(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("oi", sa.Integer(), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("und_price", sa.Float(), nullable=True),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("source_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mdt", sa.String(length=14), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "expiry", "right", "strike", name="uq_opt_quote_contract"),
    )
    _indexes("opt_quote")


def _create_opt_underlying() -> None:
    op.create_table(
        "opt_underlying",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("spot_as_of", sa.DateTime(), nullable=True),
        sa.Column("spot_source", sa.String(length=8), nullable=True),
        sa.Column("spot_user_id", sa.Integer(), nullable=True),
        sa.Column("spot_mdt", sa.String(length=14), nullable=True),
        sa.Column("atr14", sa.Float(), nullable=True),
        sa.Column("hv20", sa.Float(), nullable=True),
        sa.Column("hv60", sa.Float(), nullable=True),
        sa.Column("avg_vol20", sa.Float(), nullable=True),
        sa.Column("bars_as_of", sa.DateTime(), nullable=True),
        sa.Column("iv30", sa.Float(), nullable=True),
        sa.Column("iv_rank", sa.Float(), nullable=True),
        sa.Column("iv_pct", sa.Float(), nullable=True),
        sa.Column("iv_n", sa.Integer(), nullable=True),
        sa.Column("iv_lo", sa.Float(), nullable=True),
        sa.Column("iv_hi", sa.Float(), nullable=True),
        sa.Column("iv_as_of", sa.DateTime(), nullable=True),
        sa.Column("earnings_date", sa.String(length=10), nullable=True),
        sa.Column("earnings_src", sa.String(length=8), nullable=True),
        sa.Column("earnings_as_of", sa.DateTime(), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=True),
        sa.Column("history_done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", name="uq_opt_underlying_symbol"),
    )
    _indexes("opt_underlying")


def _create_opt_underlying_daily() -> None:
    op.create_table(
        "opt_underlying_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("on", sa.String(length=10), nullable=False),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("iv30", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "on", name="uq_opt_und_daily"),
    )
    _indexes("opt_underlying_daily")


def _create_opt_refresh_log() -> None:
    op.create_table(
        "opt_refresh_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("source_user_id", sa.Integer(), nullable=True),
        sa.Column("mdt", sa.String(length=14), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="cycle"),
        sa.Column("n_contracts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_expiries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    _indexes("opt_refresh_log")


def _create_opt_collector_status() -> None:
    op.create_table(
        "opt_collector_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=12), nullable=True),
        sa.Column("phase_detail", sa.Text(), nullable=True),
        sa.Column("gateway", sa.String(length=40), nullable=True),
        sa.Column("gateway_ok", sa.Boolean(), nullable=True),
        sa.Column("mdt", sa.String(length=14), nullable=True),
        sa.Column("cycle_n", sa.Integer(), nullable=True),
        sa.Column("cycle_started", sa.DateTime(), nullable=True),
        sa.Column("cycle_finished", sa.DateTime(), nullable=True),
        sa.Column("symbols_total", sa.Integer(), nullable=True),
        sa.Column("symbols_done", sa.Integer(), nullable=True),
        sa.Column("last_eod_on", sa.String(length=10), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("heartbeat", sa.DateTime(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(length=16), nullable=True),
    )
    _indexes("opt_collector_status")


_CREATE = {
    "opt_quote": _create_opt_quote,
    "opt_underlying": _create_opt_underlying,
    "opt_underlying_daily": _create_opt_underlying_daily,
    "opt_refresh_log": _create_opt_refresh_log,
    "opt_collector_status": _create_opt_collector_status,
}


def upgrade() -> None:
    have = _tables()
    for name in ORDER:
        if name not in have:
            _CREATE[name]()


def downgrade() -> None:
    have = _tables()
    for name in reversed(ORDER):
        if name not in have:
            continue
        existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(name)}
        for idx_name, _cols in reversed(_INDEXES[name]):
            if idx_name in existing:
                op.drop_index(idx_name, table_name=name)
        op.drop_table(name)
