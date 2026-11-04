"""The frozen view SQL in migration 0005 equals the authoring module (B6, R1 fix 4)."""

from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import Engine, text

from orchestrator.db.migrate import MIGRATIONS_DIR
from orchestrator.sql.views import VIEW_NAMES, view_statements


def _revision(name: str):  # type: ignore[no-untyped-def]
    path = MIGRATIONS_DIR / "versions" / name
    spec = importlib.util.spec_from_file_location(f"rev_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_frozen_views_match_the_authoring_module(dialect: str) -> None:
    rev = _revision("0005_eva_packets.py")
    assert tuple(rev.VIEW_NAMES) == VIEW_NAMES
    assert rev.VIEWS[dialect] == view_statements(dialect)


def test_installed_sqlite_views_are_the_frozen_text(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite stores view SQL verbatim; PostgreSQL normalises it (manifest test)")
    with engine.connect() as c:
        rows = c.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'view'"))
        stored = {r.name: r.sql for r in rows}
    for name, sql in zip(VIEW_NAMES, view_statements("sqlite"), strict=True):
        assert stored[name] == sql


def test_every_view_answers_on_an_empty_database(engine: Engine) -> None:
    with engine.connect() as c:
        for name in VIEW_NAMES:
            assert c.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() == 0
