"""Shared pytest fixtures for dashboard_tst.

Every DB fixture builds a FRESH SQLite file under pytest's ``tmp_path`` and brings
it to the Alembic head with the real migrations - never ``create_all`` - so what
the tests exercise is the migration chain the Hermes deploy runs. The app's
settings object is pointed at that file before each migration run because
``alembic/env.py`` reads ``settings.database_url`` (not the ini) for its URL.

The environment is pinned BEFORE ``app`` is imported: ``app.config`` loads
``app/.env`` on import and ``app.db`` builds its engine from the resolved URL, so
the variables below must already be in place, and ``load_dotenv`` never overrides
a variable that is already set. The URL points into pytest's own temp dir so the
module-level engine can never touch ``tst.db`` or ``tst_dev_check.db``.
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from pathlib import Path

import pytest

_TEST_TMP = Path(tempfile.gettempdir()) / "dashboard_tst_pytest"
_TEST_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TST_DATABASE_URL"] = "sqlite:///" + (_TEST_TMP / "conftest_unused.db").as_posix()
os.environ["TST_AUTH_MODE"] = "password"
os.environ["TST_ADMIN_EMAIL"] = "dev@local.test"
os.environ.setdefault("TST_ADMIN_PASSWORD", "dev-only-not-a-real-password")
os.environ["TST_IV_SEED_IBKR"] = "0"      # never probe a port or spawn the IB seeder from a test

from alembic import command                      # noqa: E402
from alembic.config import Config                # noqa: E402
from sqlalchemy import create_engine, event, inspect   # noqa: E402
from sqlalchemy.orm import sessionmaker          # noqa: E402

from app.config import settings                  # noqa: E402
from app import models                           # noqa: E402,F401  (register models on Base)

DASH_ROOT = Path(__file__).resolve().parent.parent      # dashboard_tst/
HEAD = "3b26d60468a0"
PREVIOUS_HEAD = "e2f3a4b5c6d7"


def alembic_config(url: str) -> Config:
    """An Alembic config aimed at ``url``. env.py overrides the URL from
    ``settings.database_url``, so that is set too."""
    settings.database_url = url
    cfg = Config(str(DASH_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DASH_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, rev: str = "head") -> None:
    command.upgrade(alembic_config(url), rev)


def downgrade(url: str, rev: str) -> None:
    command.downgrade(alembic_config(url), rev)


def make_engine(url: str):
    """A test engine with the same foreign-key pragma ``app.db`` installs."""
    eng = create_engine(url, connect_args={"check_same_thread": False}, future=True)

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


def table_names(url: str) -> set[str]:
    eng = make_engine(url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


@pytest.fixture
def db_url(tmp_path) -> str:
    """A URL for a fresh, not-yet-created SQLite file (no schema)."""
    return "sqlite:///" + (tmp_path / "build1.db").as_posix()


@pytest.fixture
def migrated_url(db_url) -> str:
    """The fresh DB brought to the Alembic head by the real migrations."""
    upgrade(db_url, "head")
    return db_url


@pytest.fixture
def engine(migrated_url):
    eng = make_engine(migrated_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A session on the migrated DB, closed (and rolled back) at teardown."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def user(db):
    """An approved member row to hang per-user data off."""
    u = models.User(email="member@local.test", display_name="Member",
                    role=models.ROLE_MEMBER, status=models.APPROVED,
                    created_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.timezone.utc))
    db.add(u)
    db.commit()
    return u
