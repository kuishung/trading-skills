"""SQLAlchemy engine, session factory, declarative base.

DB is chosen by ``TST_DATABASE_URL`` (config.py). SQLite for local dev,
Postgres in production -- no code change, just the env var.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Bring the DB schema to head via Alembic migrations.

    Onboarding is safe for all three states:
      - fresh DB             -> run every migration from scratch.
      - already-managed DB   -> apply any pending migrations.
      - legacy create_all DB -> create any missing (new) tables, then STAMP it
        at the baseline so future migrations apply incrementally. (This is the
        existing Hermes DB: real users/filters, no migration history yet,
        missing only the newer tables.)
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from . import models  # noqa: F401  (register models on Base)

    tables = set(inspect(engine).get_table_names())

    dash_root = Path(__file__).resolve().parent.parent  # dashboard_tst/
    cfg = Config(str(dash_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(dash_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)

    if "alembic_version" in tables:
        command.upgrade(cfg, "head")
    elif "users" in tables:
        # legacy DB built by create_all: add any new tables, then mark baseline.
        Base.metadata.create_all(bind=engine)
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
