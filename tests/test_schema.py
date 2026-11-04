"""The master schema and its baseline migration (A4, CFO decision Q4), seeds, SQLite pragmas (A5).

* The baseline migration is exactly what the master schema (``orchestrator.db.schema``) generates,
  for both dialects, and imports nothing from the application.
* A database built by the baseline has exactly the master schema's triggers, views and (on
  PostgreSQL) trigger functions, and its tables match the ORM models (``compare_metadata``).
* Downgrade and re-upgrade restore the same schema; a database with history refuses to drop.
"""

from __future__ import annotations

import ast
import importlib.util
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

from orchestrator.db import init_db, make_engine
from orchestrator.db.ddl import trigger_statements
from orchestrator.db.migrate import (
    MIGRATIONS_DIR,
    VERSION_TABLE,
    alembic_config,
    current_revision,
    head_revision,
    upgrade,
)
from orchestrator.db.models import TABLE_NAMES, Base, Setting
from orchestrator.db.schema import DIALECTS, schema_statements
from orchestrator.repository import set_setting
from orchestrator.sql.views import ALL_VIEW_NAMES, HEAD_VIEWS
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


# --------------------------------------------------------------------------- baseline = master


BASELINE = MIGRATIONS_DIR / "versions" / "0001_baseline.py"


def baseline():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("baseline_revision", BASELINE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_baseline_is_the_only_revision() -> None:
    revisions = sorted(p.name for p in (MIGRATIONS_DIR / "versions").glob("[0-9]*.py"))
    assert revisions == ["0001_baseline.py"] and head_revision() == "0001_baseline"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_baseline_is_exactly_the_master_schema(dialect: str) -> None:
    """Drift check: regenerate with ``python scripts/generate_baseline.py`` when this fails."""
    assert baseline().STATEMENTS[dialect] == schema_statements(dialect), (
        "the master schema changed: run python scripts/generate_baseline.py and commit the result"
    )


def test_the_baseline_imports_no_application_code() -> None:
    for node in ast.walk(ast.parse(BASELINE.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("orchestrator"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("orchestrator") for a in node.names)


def _installed(connection) -> dict[str, set[str]]:  # type: ignore[no-untyped-def]
    if connection.dialect.name == "sqlite":
        rows = connection.execute(
            text("SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')")
        ).all()
        return {
            "triggers": {r.name for r in rows if r.type == "trigger"},
            "views": {r.name for r in rows if r.type == "view"},
            "functions": set(),
        }
    public = "JOIN pg_namespace n ON n.oid = {} WHERE n.nspname = 'public'"
    return {
        "triggers": set(
            connection.execute(
                text(
                    "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    + public.format("c.relnamespace")
                    + " AND NOT t.tgisinternal"
                )
            ).scalars()
        ),
        "views": set(
            connection.execute(
                text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
            ).scalars()
        ),
        "functions": set(
            connection.execute(
                text("SELECT p.proname FROM pg_proc p " + public.format("p.pronamespace"))
            ).scalars()
        ),
    }


def _expected(dialect: str) -> dict[str, set[str]]:
    statements = trigger_statements(dialect)
    return {
        "triggers": {m.group(1) for s in statements if (m := re.match(r"CREATE TRIGGER (\w+)", s))},
        "views": set(ALL_VIEW_NAMES),
        "functions": {
            m.group(1)
            for s in statements
            if (m := re.match(r"CREATE OR REPLACE FUNCTION (\w+)\(", s))
        },
    }


def test_installed_schema_objects_are_the_master_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        assert _installed(connection) == _expected(engine.dialect.name)
        if engine.dialect.name == "sqlite":  # SQLite stores the trigger text verbatim
            stored = dict(
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
                ).all()
            )
            for statement in trigger_statements("sqlite"):
                name = re.match(r"CREATE TRIGGER (\w+)", statement).group(1)  # type: ignore[union-attr]
                assert stored[name] == statement


def test_downgrade_and_reupgrade_restore_the_schema(db_url: str) -> None:
    """Base → baseline → base → baseline on a database without history (R1 fix 5)."""
    engine = make_engine(db_url)
    cfg = alembic_config()

    def step(fn, rev: str) -> dict[str, set[str]]:  # type: ignore[no-untyped-def]
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            fn(cfg, rev)
        with engine.connect() as connection:
            return _installed(connection)

    try:
        first = step(command.upgrade, "head")
        assert first == _expected(engine.dialect.name)
        emptied = step(command.downgrade, "base")
        assert emptied["triggers"] == emptied["views"] == set() and not emptied["functions"]
        assert current_revision(engine) is None
        assert set(inspect(engine).get_table_names()) <= {VERSION_TABLE}
        assert step(command.upgrade, "head") == first
    finally:
        engine.dispose()


def test_a_database_with_history_refuses_to_downgrade(engine: Engine) -> None:
    cfg = alembic_config()
    with pytest.raises(RuntimeError, match="events exist"), engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "base")
    assert current_revision(engine) == head_revision()
