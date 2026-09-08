"""Purge rows orphaned while SQLite ignored ON DELETE CASCADE.

Every child table below declares ``ondelete="CASCADE"``, but SQLite only enforces
foreign keys when ``PRAGMA foreign_keys=ON`` is set per connection, and it never
was. So on SQLite (dev, and Hermes today) deleting a parent left its children
behind: a deleted curated call kept its revisions, a deleted user kept their
watchlist, drawings and calls.

That is not merely untidy. SQLite hands a new row the lowest free rowid, so the
orphans get ADOPTED by whatever row next takes that id -- a freshly curated BAP
showing three NVDA revisions in its history, seen locally on 2026-09-08. Clicking
one of those chips charts another ticker's levels under this call's name.

``app/db.py`` now turns the pragma on for every SQLite connection, so no new
orphans can appear. This migration clears the ones already there. It is written
as portable ORM-free SQL that is a harmless no-op on Postgres, which has been
enforcing these clauses all along and therefore has nothing to delete.

Revision ID: a8b9c0d1e2f3
Revises: e8f9a0b1c2d3
Create Date: 2026-09-08
"""
from __future__ import annotations

from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None

# (child table, foreign-key column, parent table) — every CASCADE child that a
# SQLite delete could have stranded.
_ORPHANS = [
    ("curated_revisions", "curated_id", "curated_tickers"),
    ("curated_tickers", "user_id", "users"),
    ("user_watchlist", "user_id", "users"),
    ("chart_drawings", "user_id", "users"),
]


def upgrade() -> None:
    conn = op.get_bind()
    existing = set(conn.dialect.get_table_names(conn))

    # 1) Orphans still pointing at an id nothing owns.
    for child, fk, parent in _ORPHANS:
        if child not in existing or parent not in existing:
            continue
        conn.exec_driver_sql(
            f"DELETE FROM {child} WHERE {fk} IS NOT NULL AND {fk} NOT IN "
            f"(SELECT id FROM {parent})"
        )

    # 2) Orphans ALREADY ADOPTED by a row that reused their old parent's id.
    #    A foreign-key check cannot see these — the id resolves. What gives them
    #    away is the clock: a call's opening revision is written in the same
    #    transaction as the call, and every later edit comes after it, so a
    #    revision can never predate the call it belongs to. One that does was
    #    written for a different, deleted call.
    if "curated_revisions" in existing and "curated_tickers" in existing:
        conn.exec_driver_sql(
            "DELETE FROM curated_revisions WHERE id IN ("
            "  SELECT r.id FROM curated_revisions r"
            "  JOIN curated_tickers t ON t.id = r.curated_id"
            "  WHERE r.created_at IS NOT NULL AND t.created_at IS NOT NULL"
            "    AND r.created_at < t.created_at)"
        )


def downgrade() -> None:
    # Deleted orphans cannot be reconstructed, and re-creating rows that pointed
    # at parents which no longer exist would only restore the bug.
    pass
