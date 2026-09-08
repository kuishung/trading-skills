"""curated_revisions: keep every version of a curated call's levels

Editing a curated call used to overwrite it, which quietly rewrote what had
actually been decided. Each version is now kept as its own row and the
curated_tickers row carries the current one; the first revision is written when the
call is created, so the history is complete rather than starting at the first edit.

Backfills one revision per existing curated_ticker for the same reason -- a call
made before this migration should not appear to have no history at all.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "curated_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("curated_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("entry", sa.Float(), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default="edit"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["curated_id"], ["curated_tickers.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_curated_revisions_curated_id", "curated_revisions",
                    ["curated_id"])

    # Seed the opening revision for calls that predate this table. Written through
    # the SQLAlchemy core (not a raw INSERT..SELECT string) so it runs the same on
    # SQLite and Postgres, per the portability rule.
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, symbol, entry, stop, target, note, created_at "
        "FROM curated_tickers"
    )).fetchall()
    if rows:
        conn.execute(
            sa.text("INSERT INTO curated_revisions "
                    "(curated_id, symbol, entry, stop, target, note, source, created_at) "
                    "VALUES (:cid, :sym, :entry, :stop, :target, :note, 'created', :ts)"),
            [{"cid": r[0], "sym": r[1], "entry": r[2], "stop": r[3], "target": r[4],
              "note": r[5], "ts": r[6]} for r in rows],
        )


def downgrade() -> None:
    op.drop_index("ix_curated_revisions_curated_id", table_name="curated_revisions")
    op.drop_table("curated_revisions")
