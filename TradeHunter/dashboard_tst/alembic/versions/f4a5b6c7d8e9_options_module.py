"""options module: option_basket, option_chain_snapshot, iv_daily, option_signal,
user_option_prefs, option_jobs, option_trades, option_trade_checks, option_idea_push
+ copy of the OPEN option_spreads rows into option_trades (strategy bull_put)

The ONE migration of the Options module (OPTIONS_MODULE_DESIGN.md II.2.1). Nine
tables, created in ORDER below and dropped in reverse; every table guarded per
table with ``get_table_names()`` because the Hermes DB predates Alembic and
``create_all`` may have run against it (the e2f3a4b5c6d7 / d1e2f3a4b5c6 pattern).

One data step: every OPEN ``option_spreads`` row becomes an ``option_trades`` row
(``strategy='bull_put'``, ``family='credit_vertical'``, two put legs,
``net_entry = -credit``, the per-trade overrides carried over,
``note='migrated from option_spreads #<id>'``). It runs ONLY in the branch that
just created ``option_trades``, so a re-run never duplicates, and it is written
with SQLAlchemy Core (``sa.table`` / ``select`` / ``insert``) - no raw SQL, no
import of ``app.models`` (a migration must not depend on the current model
classes) - so it behaves identically on SQLite and Postgres. ``option_spreads`` is
never touched, so the downgrade needs no undo for it.

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-10-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None

# Creation order; reversed on downgrade (children after parents).
ORDER = [
    "option_basket", "option_chain_snapshot", "iv_daily", "option_signal",
    "user_option_prefs", "option_jobs", "option_trades", "option_trade_checks",
    "option_idea_push",
]

# Indexes per table (name, columns), in creation order; dropped in reverse.
_INDEXES: dict[str, list[tuple[str, list[str]]]] = {
    "option_basket": [
        ("ix_option_basket_user_id", ["user_id"]),
        ("ix_option_basket_owner_key", ["owner_key"]),
        ("ix_option_basket_symbol", ["symbol"]),
    ],
    "option_chain_snapshot": [
        ("ix_ocs_symbol_day_kind", ["symbol", "snap_on", "kind"]),
        ("ix_ocs_lookup", ["symbol", "snap_on", "expiry", "right", "strike"]),
        ("ix_ocs_snap_on", ["snap_on"]),
    ],
    "iv_daily": [
        ("ix_iv_daily_symbol", ["symbol"]),
        ("ix_iv_daily_on", ["on"]),
        ("ix_iv_daily_symbol_on", ["symbol", "on"]),
    ],
    "option_signal": [
        ("ix_option_signal_user_id", ["user_id"]),
        ("ix_option_signal_hash_day", ["prefs_hash", "snap_on"]),
        ("ix_option_signal_symbol_day", ["symbol", "snap_on"]),
    ],
    "user_option_prefs": [
        ("ix_user_option_prefs_prefs_hash", ["prefs_hash"]),
    ],
    "option_jobs": [
        ("ix_option_jobs_run_on", ["run_on"]),
    ],
    "option_trades": [
        ("ix_option_trades_user_id", ["user_id"]),
        ("ix_option_trades_symbol", ["symbol"]),
        ("ix_option_trades_front_expiry", ["front_expiry"]),
    ],
    "option_trade_checks": [
        ("ix_option_trade_checks_trade_id", ["trade_id"]),
        ("ix_option_trade_checks_checked_on", ["checked_on"]),
    ],
    "option_idea_push": [
        ("ix_option_idea_push_user_id", ["user_id"]),
        ("ix_option_idea_push_symbol", ["symbol"]),
    ],
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> None:
    for name, cols in _INDEXES[table]:
        op.create_index(name, table, cols)


# ───────────────────────────────── DDL per table ─────────────────────────────────

def _create_option_basket() -> None:
    op.create_table(
        "option_basket",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("owner_key", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False, server_default="typed"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("added_on", sa.String(length=10), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("owner_key", "symbol", name="uq_option_basket_owner_symbol"),
    )
    _indexes("option_basket")


def _create_option_chain_snapshot() -> None:
    op.create_table(
        "option_chain_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snap_on", sa.String(length=10), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="eod"),
        sa.Column("source", sa.String(length=12), nullable=False, server_default="cboe"),
        sa.Column("expiry", sa.String(length=10), nullable=False),
        sa.Column("dte", sa.Integer(), nullable=False),
        sa.Column("right", sa.String(length=1), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("mid", sa.Float(), nullable=True),
        sa.Column("last", sa.Float(), nullable=True),
        sa.Column("bid_size", sa.Integer(), nullable=True),
        sa.Column("ask_size", sa.Integer(), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("rho", sa.Float(), nullable=True),
        sa.Column("theo", sa.Float(), nullable=True),
        sa.Column("oi", sa.Integer(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.Column("prev_close", sa.Float(), nullable=True),
    )
    _indexes("option_chain_snapshot")


def _create_iv_daily() -> None:
    op.create_table(
        "iv_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("on", sa.String(length=10), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="eod"),
        sa.Column("source", sa.String(length=12), nullable=False, server_default="cboe"),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("iv30", sa.Float(), nullable=True),
        sa.Column("iv30_src", sa.String(length=8), nullable=True),
        sa.Column("atm_iv30", sa.Float(), nullable=True),
        sa.Column("hv20", sa.Float(), nullable=True),
        sa.Column("hv60", sa.Float(), nullable=True),
        sa.Column("iv_hv_premium", sa.Float(), nullable=True),
        sa.Column("iv_rank", sa.Float(), nullable=True),
        sa.Column("iv_pct", sa.Float(), nullable=True),
        sa.Column("iv_n", sa.Integer(), nullable=True),
        sa.Column("iv_state", sa.String(length=8), nullable=True),
        sa.Column("iv_lo", sa.Float(), nullable=True),
        sa.Column("iv_hi", sa.Float(), nullable=True),
        sa.Column("iv_by_expiry", sa.JSON(), nullable=True),
        sa.Column("iv_front", sa.Float(), nullable=True),
        sa.Column("iv_back", sa.Float(), nullable=True),
        sa.Column("term_ratio", sa.Float(), nullable=True),
        sa.Column("skew25", sa.Float(), nullable=True),
        sa.Column("skew_norm", sa.Float(), nullable=True),
        sa.Column("expected_move", sa.Float(), nullable=True),
        sa.Column("earnings_date", sa.String(length=10), nullable=True),
        sa.Column("earnings_days", sa.Integer(), nullable=True),
        sa.Column("n_contracts", sa.Integer(), nullable=True),
        sa.Column("n_expiries", sa.Integer(), nullable=True),
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "on", name="uq_iv_daily_day"),
    )
    _indexes("iv_daily")


def _create_option_signal() -> None:
    op.create_table(
        "option_signal",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snap_on", sa.String(length=10), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="eod"),
        sa.Column("as_of", sa.DateTime(), nullable=True),
        sa.Column("prefs_hash", sa.String(length=16), nullable=False),
        sa.Column("engine_version", sa.String(length=12), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="ok"),
        sa.Column("trend", sa.String(length=12), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("setup", sa.JSON(), nullable=True),
        sa.Column("iv", sa.JSON(), nullable=True),
        sa.Column("strategies", sa.JSON(), nullable=True),
        sa.Column("picks", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("computed_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("symbol", "snap_on", "kind", "prefs_hash", name="uq_option_signal_key"),
    )
    _indexes("option_signal")


def _create_user_option_prefs() -> None:
    op.create_table(
        "user_option_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prefs", sa.JSON(), nullable=False),
        sa.Column("prefs_hash", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_user_option_prefs_user"),
    )
    _indexes("user_option_prefs")


def _create_option_jobs() -> None:
    op.create_table(
        "option_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(length=12), nullable=False, server_default="nightly"),
        sa.Column("run_on", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pushed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    _indexes("option_jobs")


def _create_option_trades() -> None:
    op.create_table(
        "option_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("strategy", sa.String(length=24), nullable=False),
        sa.Column("family", sa.String(length=24), nullable=False),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("front_expiry", sa.String(length=10), nullable=False),
        sa.Column("back_expiry", sa.String(length=10), nullable=True),
        sa.Column("net_entry", sa.Float(), nullable=False),
        sa.Column("contracts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_loss", sa.Float(), nullable=True),
        sa.Column("chart_stop", sa.Float(), nullable=True),
        sa.Column("chart_target", sa.Float(), nullable=True),
        sa.Column("roll_dte", sa.Integer(), nullable=True),
        sa.Column("paper", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("earnings_date_at_entry", sa.String(length=10), nullable=True),
        sa.Column("roll_delta", sa.Float(), nullable=True),
        sa.Column("loss_stop_pct", sa.Float(), nullable=True),
        sa.Column("profit_target_pct", sa.Float(), nullable=True),
        sa.Column("dte_floor", sa.Integer(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("close_reason", sa.String(length=24), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    _indexes("option_trades")


def _create_option_trade_checks() -> None:
    op.create_table(
        "option_trade_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("option_trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("checked_on", sa.String(length=10), nullable=False),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("mark", sa.Float(), nullable=True),
        sa.Column("pl", sa.Float(), nullable=True),
        sa.Column("loss_pct", sa.Float(), nullable=True),
        sa.Column("profit_pct", sa.Float(), nullable=True),
        sa.Column("dte", sa.Integer(), nullable=True),
        sa.Column("back_dte", sa.Integer(), nullable=True),
        sa.Column("net_delta", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("legs", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=10), nullable=False, server_default="UNKNOWN"),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=True),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=12), nullable=False, server_default="cboe"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("trade_id", "checked_on", name="uq_option_trade_check_day"),
    )
    _indexes("option_trade_checks")


def _create_option_idea_push() -> None:
    op.create_table(
        "option_idea_push",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("idea_key", sa.String(length=80), nullable=False),
        sa.Column("short_strike", sa.Float(), nullable=True),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("user_id", "idea_key", name="uq_option_idea_push"),
    )
    _indexes("option_idea_push")


_CREATE = {
    "option_basket": _create_option_basket,
    "option_chain_snapshot": _create_option_chain_snapshot,
    "iv_daily": _create_iv_daily,
    "option_signal": _create_option_signal,
    "user_option_prefs": _create_user_option_prefs,
    "option_jobs": _create_option_jobs,
    "option_trades": _create_option_trades,
    "option_trade_checks": _create_option_trade_checks,
    "option_idea_push": _create_option_idea_push,
}


# ──────────────────────────── the data step (Core only) ────────────────────────────

# Every column the copy reads from option_spreads. The later revisions
# (b9c0d1e2f3a4, c0d1e2f3a4b5) added the override and per-leg columns; a column
# missing on an odd DB is read as None rather than failing the whole upgrade.
_SPREAD_COLS = (
    "id", "user_id", "symbol", "expiry", "short_strike", "long_strike", "credit",
    "contracts", "entry_delta", "opened_at", "status", "roll_delta", "loss_stop_pct",
    "profit_target_pct", "dte_floor", "short_price", "long_price",
    "short_entry_delta", "long_entry_delta", "entry_iv",
)


def _signed_put_delta(value) -> float | None:
    """option_spreads stores put deltas as ABSOLUTE values; the Leg shape carries
    them signed as the feed gives them (a put's delta is negative)."""
    if value is None:
        return None
    try:
        return -abs(float(value))
    except (TypeError, ValueError):
        return None


def _leg(expiry: str, strike: float, side: str, price, delta, iv) -> dict:
    """The stored Leg shape (II.2.8) + entry_price / entry_delta / entry_iv. The
    old row kept only the fill, so ``price`` (the mid at entry) is the fill too and
    bid / ask / oi / volume were never recorded."""
    return {
        "expiry": expiry, "right": "P", "strike": float(strike), "side": side, "qty": 1,
        "price": price, "bid": None, "ask": None, "iv": iv, "delta": delta,
        "oi": None, "volume": None,
        "entry_price": price, "entry_delta": delta, "entry_iv": iv,
    }


def _copy_open_spreads() -> int:
    """Every option_spreads row with status 'open' becomes an option_trades row:
    strategy 'bull_put', family 'credit_vertical', legs [sell P short, buy P long]
    at the row's expiry with entry_price / entry_delta / entry_iv from the stored
    entry fields, qty / credit / overrides / opened_at carried over, note
    'migrated from option_spreads #<id>'. Returns the number of rows copied."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "option_spreads" not in set(insp.get_table_names()):
        return 0
    present = {c["name"] for c in insp.get_columns("option_spreads")}
    cols = [c for c in _SPREAD_COLS if c in present]
    # opened_at is typed so SQLite's stored text comes back as a datetime (the
    # typed DateTime insert below rejects a bare string); the rest need no type.
    spreads = sa.table("option_spreads",
                       *[sa.column(c, sa.DateTime()) if c == "opened_at" else sa.column(c)
                         for c in cols])
    status_col = spreads.c.status if "status" in present else None

    stmt = sa.select(*[spreads.c[c] for c in cols])
    if status_col is not None:
        stmt = stmt.where(status_col == "open")
    rows = bind.execute(stmt).mappings().all()
    if not rows:
        return 0

    trades = sa.table(
        "option_trades",
        sa.column("user_id", sa.Integer()), sa.column("symbol", sa.String()),
        sa.column("strategy", sa.String()), sa.column("family", sa.String()),
        sa.column("legs", sa.JSON()), sa.column("front_expiry", sa.String()),
        sa.column("back_expiry", sa.String()), sa.column("net_entry", sa.Float()),
        sa.column("contracts", sa.Integer()), sa.column("max_loss", sa.Float()),
        sa.column("chart_stop", sa.Float()), sa.column("chart_target", sa.Float()),
        sa.column("roll_dte", sa.Integer()), sa.column("paper", sa.Boolean()),
        sa.column("signal_id", sa.Integer()), sa.column("earnings_date_at_entry", sa.String()),
        sa.column("roll_delta", sa.Float()), sa.column("loss_stop_pct", sa.Float()),
        sa.column("profit_target_pct", sa.Float()), sa.column("dte_floor", sa.Integer()),
        sa.column("meta", sa.JSON()), sa.column("opened_at", sa.DateTime()),
        sa.column("status", sa.String()), sa.column("closed_at", sa.DateTime()),
        sa.column("close_reason", sa.String()), sa.column("note", sa.Text()),
    )

    out = []
    for r in rows:
        g = r.get
        short_k, long_k = float(g("short_strike")), float(g("long_strike"))
        short_px, long_px = g("short_price"), g("long_price")
        credit = g("credit")
        if credit is None and short_px is not None and long_px is not None:
            credit = float(short_px) - float(long_px)      # the derived credit, as the Portfolio page shows it
        note = "migrated from option_spreads #%d" % int(g("id"))
        if credit is None:
            net_entry = 0.0                                # NOT NULL; the row says so instead of inventing a figure
            note += " (credit unknown)"
        else:
            net_entry = -float(credit)
        width = abs(short_k - long_k)
        max_loss = round((width - float(credit)) * 100.0, 2) if credit is not None else None
        short_delta = _signed_put_delta(g("short_entry_delta") if g("short_entry_delta") is not None
                                        else g("entry_delta"))
        long_delta = _signed_put_delta(g("long_entry_delta"))
        entry_iv = g("entry_iv")
        legs = [
            _leg(g("expiry"), short_k, "sell", short_px, short_delta, entry_iv),
            _leg(g("expiry"), long_k, "buy", long_px, long_delta, None),
        ]
        out.append({
            "user_id": g("user_id"), "symbol": g("symbol"),
            "strategy": "bull_put", "family": "credit_vertical",
            "legs": legs, "front_expiry": g("expiry"), "back_expiry": None,
            "net_entry": net_entry, "contracts": int(g("contracts") or 1),
            "max_loss": max_loss, "chart_stop": None, "chart_target": None,
            "roll_dte": None, "paper": False, "signal_id": None,
            "earnings_date_at_entry": None,
            "roll_delta": g("roll_delta"), "loss_stop_pct": g("loss_stop_pct"),
            "profit_target_pct": g("profit_target_pct"), "dte_floor": g("dte_floor"),
            "meta": {}, "opened_at": g("opened_at"), "status": "open",
            "closed_at": None, "close_reason": None, "note": note,
        })
    bind.execute(sa.insert(trades), out)
    return len(out)


# ─────────────────────────────────── upgrade / downgrade ───────────────────────────────────

def upgrade() -> None:
    have = _tables()
    created: set[str] = set()
    for name in ORDER:
        if name in have:
            continue
        _CREATE[name]()
        created.add(name)
    # The data step runs ONLY in the branch that just created option_trades, so a
    # second run of this revision (or a create_all-prepared DB) never duplicates.
    if "option_trades" in created:
        _copy_open_spreads()


def downgrade() -> None:
    have = _tables()
    for name in reversed(ORDER):
        if name not in have:
            continue
        for idx_name, _cols in reversed(_INDEXES[name]):
            op.drop_index(idx_name, table_name=name)
        op.drop_table(name)
    # option_spreads was never touched, so the copy needs no undo.
