"""Raw-SQL helpers for database-level tests (the triggers are never disabled)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from orchestrator.actors import Actor
from orchestrator.db.ddl import is_immutability_violation, is_transition_violation
from orchestrator.db.models import (
    EvaluationPass,
)


def raw(engine: Engine, sql: str, **params: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def assert_db_rejects(engine: Engine, sql: str, *, either: bool = False, **params: object) -> None:
    """The database rejects ``sql`` through an immutability trigger (``either``: or through the
    0007 transition-table trigger, when both rules forbid the change)."""
    with pytest.raises(DBAPIError) as info:
        raw(engine, sql, **params)
    ok = is_immutability_violation(info.value)
    if either:
        ok = ok or is_transition_violation(info.value)
    assert ok, info.value


LLM = Actor.llm("model-a")


def row_of(engine: Engine, table: str, row_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT * FROM {table} WHERE id = :id"), {"id": row_id})
        return dict(result.mappings().one())


def insert_row(engine: Engine, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    params = ", ".join(f":{c}" for c in values)
    values = {k: json.dumps(v) if isinstance(v, dict | list) else v for k, v in values.items()}
    raw(engine, f"INSERT INTO {table} ({columns}) VALUES ({params})", **values)


def assert_db_rejects_insert(engine: Engine, values: dict[str, Any]) -> None:
    with pytest.raises(DBAPIError) as info:
        insert_row(engine, "packets", values)
    assert is_immutability_violation(info.value), info.value


def make_pass(packet_id: str, cycle: str | None = "c1") -> EvaluationPass:
    return EvaluationPass(
        packet_id=packet_id,
        eval_cycle_id=cycle,
        field_name="styles",
        model="fake-a",
        pass_no=1,
        role="extract",
        value=["plan"],
    )
