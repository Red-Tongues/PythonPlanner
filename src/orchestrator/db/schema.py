"""The master schema: one place that defines the whole database, per dialect.

* **Tables, columns, constraints and indexes** — ``orchestrator.db.models`` (SQLAlchemy 2.0).
  A new column is written there, once.
* **Triggers** (and PostgreSQL trigger functions) — ``orchestrator.db.ddl``: the immutability,
  lineage, append-only and transition-table guarantees (Requirement F; golden rules 1 and 7).
* **Views** — ``orchestrator.sql.views``.

``schema_statements(dialect)`` turns those three into the ordered DDL that builds an empty
database. The baseline migration holds that text, written by ``scripts/generate_baseline.py``;
``tests/test_schema.py`` fails if the baseline and this module ever differ, and also checks the
built database against the models. Seed rows are not schema: ``orchestrator.db.seed`` inserts
them on every ``init-db``.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable
from sqlalchemy.sql.ddl import ExecutableDDLElement

from orchestrator.db.ddl import trigger_statements
from orchestrator.db.models import Base
from orchestrator.sql.views import all_view_statements

DIALECTS: tuple[str, ...] = ("sqlite", "postgresql")
_DIALECTS: dict[str, Dialect] = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.psycopg.dialect(),
}


def table_statements(dialect: str) -> list[str]:
    """``CREATE TABLE`` and ``CREATE INDEX`` text for every model, compiled for ``dialect``, in a
    fixed order: tables by dependency, each followed by its indexes sorted by name.

    A foreign key marked ``use_alter`` (the one FK cycle) stays inline on SQLite, which accepts a
    reference to a table created later; on PostgreSQL it is added by ``ALTER TABLE`` at the end.
    """
    compiled_for = _DIALECTS[dialect]
    statements: list[str] = []
    deferred: list[ForeignKeyConstraint] = []
    for table in Base.metadata.sorted_tables:
        inline = [
            fk for fk in table.foreign_key_constraints if dialect == "sqlite" or not fk.use_alter
        ]
        deferred += [fk for fk in table.foreign_key_constraints if fk not in inline]
        statements.append(
            _text(CreateTable(table, include_foreign_key_constraints=inline), compiled_for)
        )
        for index in sorted(table.indexes, key=lambda ix: ix.name or ""):
            statements.append(_text(CreateIndex(index), compiled_for))
    for fk in deferred:
        # AddConstraint marks the constraint "not inline" for good; restore that, or a later
        # SQLite CREATE TABLE in the same process would silently drop the foreign key.
        rule = fk._create_rule
        statements.append(_text(AddConstraint(fk), compiled_for))
        fk._create_rule = rule
    return statements


def _text(element: ExecutableDDLElement, dialect: Dialect) -> str:
    text = str(element.compile(dialect=dialect)).strip()
    return "\n".join(line.rstrip() for line in text.splitlines())


def schema_statements(dialect: str) -> list[str]:
    """Every statement that builds the schema on an empty database: tables, triggers, views."""
    if dialect not in DIALECTS:
        raise NotImplementedError(f"no schema for dialect {dialect!r}")
    return table_statements(dialect) + trigger_statements(dialect) + all_view_statements(dialect)
