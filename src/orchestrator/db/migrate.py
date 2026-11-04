"""Versioned migrations (A4): Alembic applies the schema (tables, triggers, views).

The history is one generated baseline (``migrations/versions/0001_baseline.py``) built from the
master schema in ``orchestrator.db.schema``; the applied revision is recorded in the
``schema_version`` table. ``orchestrator init-db`` calls ``upgrade(engine)`` and then seeds.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

log = logging.getLogger(__name__)

VERSION_TABLE = "schema_version"
MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        ctx = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
        return ctx.get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> str | None:
    """Apply migrations up to ``revision`` in one transaction. Returns the new revision."""
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    current = current_revision(engine)
    log.info("database at revision %s", current)
    return current
