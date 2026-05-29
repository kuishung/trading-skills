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
    """Create tables. Scaffold uses metadata.create_all; a real migration
    tool (Alembic) is the Phase 1 follow-up before the schema stabilises."""
    from . import models  # noqa: F401  (ensure models are registered on Base)

    Base.metadata.create_all(bind=engine)
