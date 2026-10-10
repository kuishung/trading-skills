"""Alembic environment for the Options Screener's own database
(OPTIONS_SCREENER_DESIGN.md §2-§3).

* ``target_metadata`` = ``ScrBase.metadata`` (``app/screener_models.py``) - only the
  ``scr_*`` tables, never the platform tables.
* The URL is the ini's ``sqlalchemy.url`` (``app/screener_db.alembic_config`` sets it),
  else ``screener_db.database_url()`` (``TST_SCREENER_DATABASE_URL`` or ``screener.db``
  beside ``tst.db``).
* The history lives in ``alembic_version_screener`` so this environment can share one
  Postgres database with the main one (``alembic_version``).
* ``render_as_batch=True``: ALTERs work on SQLite (the table is rebuilt under the hood).
* No ``fileConfig``: the web app and the collector run these migrations in-process, and
  a logging reset there would drop their handlers.
"""
from __future__ import annotations

import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `app` importable when alembic runs from the dashboard_tst/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import screener_db  # noqa: E402
from app.screener_models import ScrBase  # noqa: E402

config = context.config
_url = (config.get_main_option("sqlalchemy.url") or "").strip() or screener_db.database_url()
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = ScrBase.metadata
VERSION_TABLE = screener_db.VERSION_TABLE        # "alembic_version_screener"


def run_migrations_offline() -> None:
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": _url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
            version_table=VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
