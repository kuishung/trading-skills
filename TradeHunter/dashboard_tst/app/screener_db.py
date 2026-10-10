"""The Options Screener's own database (OPTIONS_SCREENER_DESIGN.md §2-§3).

``TST_SCREENER_DATABASE_URL`` picks it; the default is ``screener.db`` beside ``tst.db``
(an absolute path, so the web app and the Hermes collector agree whatever their working
directory). On Postgres it may point at the same database as ``TST_DATABASE_URL``: the
tables are ``scr_*`` and the migration history is kept in ``alembic_version_screener``.

The engine is built lazily (first use) so tests can aim it elsewhere with ``configure``.
Schema changes go through ``alembic_screener/`` - ``init_screener_db`` runs ``upgrade
head`` (the web app at startup, the collector before its first write).
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

DASH_ROOT = Path(__file__).resolve().parent.parent           # dashboard_tst/
ENV_URL = "TST_SCREENER_DATABASE_URL"
VERSION_TABLE = "alembic_version_screener"

_lock = threading.Lock()
_state: dict = {"url": None, "engine": None, "Session": None}


def default_url() -> str:
    return "sqlite:///" + (DASH_ROOT / "screener.db").as_posix()


def database_url() -> str:
    return (_state["url"] or os.environ.get(ENV_URL) or "").strip() or default_url()


def _build(url: str):
    is_sqlite = url.startswith("sqlite")
    eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
                        future=True, pool_pre_ping=not is_sqlite)
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            # WAL: the web app reads while the collector replaces a symbol's rows
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return eng


def configure(url: str | None) -> None:
    """Aim the module at ``url`` (None = back to the environment / default). Disposes the
    old engine. For tests and one-off tools."""
    with _lock:
        if _state["engine"] is not None:
            _state["engine"].dispose()
        _state.update(url=url, engine=None, Session=None)


def engine():
    with _lock:
        if _state["engine"] is None:
            _state["engine"] = _build(database_url())
            _state["Session"] = sessionmaker(bind=_state["engine"], autoflush=False,
                                             autocommit=False, future=True)
        return _state["engine"]


def SessionLocal():  # noqa: N802 - mirrors app.db.SessionLocal
    engine()
    return _state["Session"]()


@contextlib.contextmanager
def session():
    """``with screener_db.session() as s:`` - closed (and rolled back if left open) after."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def alembic_config(url: str | None = None):
    from alembic.config import Config

    cfg = Config(str(DASH_ROOT / "alembic_screener.ini"))
    cfg.set_main_option("script_location", str(DASH_ROOT / "alembic_screener"))
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    return cfg


def init_screener_db() -> None:
    """Bring the screener database to head (creates the file on first use)."""
    from alembic import command

    command.upgrade(alembic_config(), "head")
