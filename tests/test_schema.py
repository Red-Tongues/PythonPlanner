"""Schema, migrations (A4), seeds and SQLite pragmas (A5)."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

from orchestrator.db import init_db, make_engine
from orchestrator.db.ddl import HEAD_VIEWS
from orchestrator.db.migrate import VERSION_TABLE, current_revision, head_revision, upgrade
from orchestrator.db.models import TABLE_NAMES, Base, Setting
from orchestrator.repository import set_setting
from tests.conftest import USER


def test_upgrade_from_empty_reaches_head(db_url: str) -> None:
    engine = make_engine(db_url)
    try:
        assert current_revision(engine) is None
        assert upgrade(engine) == head_revision()
        tables = set(inspect(engine).get_table_names())
        assert set(TABLE_NAMES) | {VERSION_TABLE} <= tables
        assert set(HEAD_VIEWS) <= set(inspect(engine).get_view_names())
        # re-running is a no-op
        assert upgrade(engine) == head_revision()
    finally:
        engine.dispose()


def test_models_and_migrations_do_not_drift(engine: Engine) -> None:
    """The ORM models and the migration history describe the same schema."""
    with engine.connect() as connection:
        ctx = MigrationContext.configure(
            connection, opts={"compare_type": True, "version_table": VERSION_TABLE}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_init_db_is_idempotent(engine: Engine) -> None:
    summary = init_db(engine)
    assert summary["revision_before"] == summary["revision"] == head_revision()
    assert summary["fields"] == {"inserted": [], "updated": [], "disabled": []}


def test_seeding_does_not_overwrite_user_values(engine: Engine, session: Session) -> None:
    set_setting(session, "aggregation.dispute_margin", 0.2, actor=USER)
    session.commit()
    init_db(engine)
    session.expire_all()
    assert session.get(Setting, "aggregation.dispute_margin").value == 0.2


def test_sqlite_pragmas(db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        return
    engine = make_engine(db_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()
