"""Alembic environment. Always invoked programmatically with an open connection."""

from __future__ import annotations

from alembic import context

from orchestrator.db.migrate import VERSION_TABLE
from orchestrator.db.models import Base

config = context.config
connection = config.attributes.get("connection")
if connection is None:  # pragma: no cover - guard for direct `alembic` CLI use
    raise RuntimeError("run migrations through orchestrator.db.migrate.upgrade(engine)")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table=VERSION_TABLE,
    render_as_batch=connection.dialect.name == "sqlite",
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
