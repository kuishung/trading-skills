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

    Which state we are in is decided by the STAMPED REVISION, not by whether an
    alembic_version table exists. A table that exists but holds no row means
    nothing is stamped, so an upgrade would replay from base against tables that
    are already there -- "table matp_history already exists", and the app fails to
    boot. That state is reachable (an interrupted stamp, a downgrade to base, a
    copied DB) and was hit locally on 2026-09-07; an empty version table is
    treated as unmanaged, which is what it is.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401  (register models on Base)

    tables = set(inspect(engine).get_table_names())

    stamped = None
    if "alembic_version" in tables:
        try:
            with engine.connect() as conn:
                row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            stamped = row[0] if row else None
        except Exception:  # noqa: BLE001
            stamped = None

    dash_root = Path(__file__).resolve().parent.parent  # dashboard_tst/
    cfg = Config(str(dash_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(dash_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)

    if stamped:
        command.upgrade(cfg, "head")
    elif "users" in tables:
        # legacy DB built by create_all: add any new tables, then mark baseline.
        Base.metadata.create_all(bind=engine)
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
