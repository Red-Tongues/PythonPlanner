"""``INSERT … ON CONFLICT DO NOTHING`` through SQLAlchemy's typed dialect inserts (portable to
SQLite and PostgreSQL), so column types — UTC datetimes included — are bound exactly as the ORM
binds them."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session


def insert_if_absent(
    session: Session, table: Table, values: dict[str, Any], conflict: Sequence[str]
) -> None:
    dialect = session.get_bind().dialect.name
    module = {"sqlite": sqlite, "postgresql": postgresql}[dialect]
    stmt = module.insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict)
    session.connection().execute(stmt)
