"""Views (B6): the baseline carries every master view and trigger; installed views are the
master text; every view answers on an empty database."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from orchestrator.db.ddl import trigger_statements
from orchestrator.sql.views import (
    ALL_VIEW_NAMES,
    HEAD_VIEWS,
    RUN_VIEWS,
    VIEW_NAMES,
    all_view_statements,
)


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_the_baseline_carries_every_master_view(dialect: str) -> None:
    from tests.test_schema import baseline

    statements = baseline().STATEMENTS[dialect]
    for sql in all_view_statements(dialect):
        assert sql in statements
    assert (*HEAD_VIEWS, *VIEW_NAMES, *RUN_VIEWS) == ALL_VIEW_NAMES


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_the_baseline_carries_every_master_trigger(dialect: str) -> None:
    from tests.test_schema import baseline

    statements = baseline().STATEMENTS[dialect]
    assert [s for s in statements if s.startswith(("CREATE TRIGGER", "CREATE OR REPLACE"))] == (
        trigger_statements(dialect)
    )


def test_installed_sqlite_views_are_the_master_text(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite stores view SQL verbatim; PostgreSQL normalises it (manifest test)")
    with engine.connect() as c:
        rows = c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'"))
        stored = {r.name: r.sql for r in rows}
    for name, sql in zip(ALL_VIEW_NAMES, all_view_statements("sqlite"), strict=True):
        assert stored[name] == sql


def test_every_view_answers_on_an_empty_database(engine: Engine) -> None:
    with engine.connect() as c:
        for name in ALL_VIEW_NAMES:
            assert c.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() == 0
