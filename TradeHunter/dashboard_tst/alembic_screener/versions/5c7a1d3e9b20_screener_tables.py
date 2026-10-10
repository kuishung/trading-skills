"""screener tables: scr_contract, scr_underlying, scr_underlying_daily, scr_universe,
scr_pass, scr_status

The Options Screener's market tables (OPTIONS_SCREENER_DESIGN.md §3) in their OWN
database (``app/screener_db.py``), mirroring ``app/screener_models.py`` exactly: columns,
types, nullability, the unique constraints and the ``scr_contract`` symbol index. The
first revision of the ``alembic_screener`` environment (history table
``alembic_version_screener``). Portable types only (SQLite dev, Postgres later).

Revision ID: 5c7a1d3e9b20
Revises:
Create Date: 2026-10-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "5c7a1d3e9b20"
down_revision = None
branch_labels = None
depends_on = None

ORDER = ["scr_contract", "scr_underlying", "scr_underlying_daily", "scr_universe", "scr_pass",
         "scr_status"]


def upgrade() -> None:
    op.create_table(
        "scr_contract",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("expiry", sa.String(length=10), nullable=False),
        sa.Column("right", sa.String(length=1), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("weekly", sa.Boolean(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("last", sa.Float(), nullable=True),
        sa.Column("chg_pct", sa.Float(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("oi", sa.Integer(), nullable=True),
        sa.Column("vol_prev", sa.Integer(), nullable=True),
        sa.Column("oi_prev", sa.Integer(), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("last_trade", sa.DateTime(), nullable=True),
        sa.Column("session", sa.String(length=10), nullable=False),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("symbol", "expiry", "right", "strike", name="uq_scr_contract"),
    )
    op.create_index("ix_scr_contract_symbol", "scr_contract", ["symbol"])

    op.create_table(
        "scr_underlying",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=True),
        sa.Column("sec_type", sa.String(length=8), nullable=True),
        sa.Column("exchange", sa.String(length=8), nullable=True),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("spot_src", sa.String(length=8), nullable=True),
        sa.Column("spot_as_of", sa.DateTime(), nullable=True),
        sa.Column("prev_close", sa.Float(), nullable=True),
        sa.Column("chg_pct", sa.Float(), nullable=True),
        sa.Column("stock_volume", sa.Float(), nullable=True),
        sa.Column("avg_vol20", sa.Float(), nullable=True),
        sa.Column("avg_vol50", sa.Float(), nullable=True),
        sa.Column("sma20", sa.Float(), nullable=True),
        sa.Column("sma50", sa.Float(), nullable=True),
        sa.Column("sma200", sa.Float(), nullable=True),
        sa.Column("rsi14", sa.Float(), nullable=True),
        sa.Column("atr14", sa.Float(), nullable=True),
        sa.Column("atr_pct", sa.Float(), nullable=True),
        sa.Column("hv20", sa.Float(), nullable=True),
        sa.Column("hv60", sa.Float(), nullable=True),
        sa.Column("hi52", sa.Float(), nullable=True),
        sa.Column("lo52", sa.Float(), nullable=True),
        sa.Column("perf5", sa.Float(), nullable=True),
        sa.Column("perf20", sa.Float(), nullable=True),
        sa.Column("trend", sa.String(length=10), nullable=True),
        sa.Column("iv30", sa.Float(), nullable=True),
        sa.Column("iv30_prev", sa.Float(), nullable=True),
        sa.Column("iv_rank", sa.Float(), nullable=True),
        sa.Column("iv_pct", sa.Float(), nullable=True),
        sa.Column("iv_hi", sa.Float(), nullable=True),
        sa.Column("iv_lo", sa.Float(), nullable=True),
        sa.Column("iv_n", sa.Integer(), nullable=True),
        sa.Column("exp_move30", sa.Float(), nullable=True),
        sa.Column("call_vol", sa.Integer(), nullable=True),
        sa.Column("put_vol", sa.Integer(), nullable=True),
        sa.Column("call_oi", sa.Integer(), nullable=True),
        sa.Column("put_oi", sa.Integer(), nullable=True),
        sa.Column("n_contracts", sa.Integer(), nullable=True),
        sa.Column("earnings_date", sa.String(length=10), nullable=True),
        sa.Column("earnings_src", sa.String(length=8), nullable=True),
        sa.Column("bars_as_of", sa.DateTime(), nullable=True),
        sa.Column("history_done", sa.Boolean(), nullable=False),
        sa.Column("history_tries", sa.Integer(), nullable=False),
        sa.Column("history_next", sa.DateTime(), nullable=True),
        sa.Column("pass_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", name="uq_scr_underlying_symbol"),
    )

    op.create_table(
        "scr_underlying_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("on", sa.String(length=10), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("close_raw", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("iv30", sa.Float(), nullable=True),
        sa.UniqueConstraint("symbol", "on", name="uq_scr_und_daily"),
    )

    op.create_table(
        "scr_universe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("n_contracts", sa.Integer(), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=True),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("symbol", name="uq_scr_universe_symbol"),
    )

    op.create_table(
        "scr_pass",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("session", sa.String(length=10), nullable=True),
        sa.Column("started", sa.DateTime(), nullable=False),
        sa.Column("finished", sa.DateTime(), nullable=True),
        sa.Column("n_symbols", sa.Integer(), nullable=True),
        sa.Column("n_ok", sa.Integer(), nullable=True),
        sa.Column("n_failed", sa.Integer(), nullable=True),
        sa.Column("n_contracts", sa.Integer(), nullable=True),
        sa.Column("requests", sa.Integer(), nullable=True),
        sa.Column("ms", sa.Integer(), nullable=True),
    )

    op.create_table(
        "scr_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=10), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("heartbeat", sa.DateTime(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(length=16), nullable=True),
        sa.Column("pass_id", sa.Integer(), nullable=True),
        sa.Column("symbols_total", sa.Integer(), nullable=True),
        sa.Column("symbols_done", sa.Integer(), nullable=True),
        sa.Column("universe_n", sa.Integer(), nullable=True),
        sa.Column("universe_on", sa.String(length=10), nullable=True),
        sa.Column("history_done_n", sa.Integer(), nullable=True),
        sa.Column("history_total", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("api_ok", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_index("ix_scr_contract_symbol", table_name="scr_contract")
    for name in reversed(ORDER):
        op.drop_table(name)
