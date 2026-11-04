"""Shared fixtures.

A6 — dual-dialect fixture: ``db_url`` is parametrised so every database test runs on SQLite and,
when ``ORCH_TEST_POSTGRES_URL`` is set (CI sets it), on PostgreSQL too. The PostgreSQL params also
carry the ``postgres`` marker, so ``pytest -m postgres`` selects exactly the PostgreSQL runs.
The immutability triggers are never disabled here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.actors import Actor
from orchestrator.contracts import ProjectTree
from orchestrator.db import init_db, make_engine, make_session_factory
from orchestrator.ingest import persist_tree

POSTGRES_URL = os.environ.get("ORCH_TEST_POSTGRES_URL")
USER = Actor.user("test")
KERNEL = Actor.system("kernel")

DIALECT_PARAMS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=[
            pytest.mark.postgres,
            pytest.mark.skipif(not POSTGRES_URL, reason="ORCH_TEST_POSTGRES_URL not set"),
        ],
    ),
]


def _reset_postgres(url: str) -> None:
    engine = make_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(params=DIALECT_PARAMS)
def db_url(request: pytest.FixtureRequest, tmp_path) -> str:  # type: ignore[no-untyped-def]
    """A fresh, *empty* database URL for this test."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'test.db'}"
    assert POSTGRES_URL is not None
    _reset_postgres(POSTGRES_URL)
    return POSTGRES_URL


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """A fresh database at the head schema revision, seeded."""
    eng = make_engine(db_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    s = factory()
    yield s
    s.rollback()
    s.close()


SMALL_TREE = {
    "project": {"name": "Demo project", "brief": "A tiny project for tests."},
    "objectives": [
        {
            "name": "Build",
            "brief": "",
            "tasks": [
                {"title": "Write the parser", "prompt_description": "Parse things."},
                {"title": "Write the CLI", "prompt_description": "Expose things."},
            ],
        },
        {
            "name": "Ship",
            "tasks": [{"title": "Release", "prompt_description": "Tag and publish."}],
        },
    ],
}


@pytest.fixture
def tree(session: Session):  # type: ignore[no-untyped-def]
    """The SMALL_TREE persisted and committed; returns the IngestResult."""
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    session.commit()
    return result
