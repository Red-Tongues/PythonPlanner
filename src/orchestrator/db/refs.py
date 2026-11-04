"""Human-readable lineage refs: ``P-0001``, ``O-0001-01`` (objectives), ``PP-000001`` (packets).

A ref names a lineage; every version of the record keeps it (``(ref, version)`` is unique).

Allocation is atomic (R1 fix 6): each counter is one ``ref_counters`` row, created on first use
and incremented with ``UPDATE … RETURNING`` inside the caller's transaction. A concurrent writer
waits for the row lock (PostgreSQL) or the write lock (SQLite, up to ``busy_timeout``), so two
transactions can never receive the same number, and numbers are never reused after a delete.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

_CREATE = text(
    "INSERT INTO ref_counters (name, value) VALUES (:name, 0) ON CONFLICT (name) DO NOTHING"
)
_BUMP = text("UPDATE ref_counters SET value = value + 1 WHERE name = :name RETURNING value")


def allocate(session: Session, counter: str) -> int:
    """Return the next number of ``counter`` (1, 2, …) within the current transaction."""
    session.execute(_CREATE, {"name": counter})
    return int(session.execute(_BUMP, {"name": counter}).scalar_one())


def next_project_ref(session: Session) -> str:
    return f"P-{allocate(session, 'project'):04d}"


def next_objective_ref(session: Session, project_ref: str) -> str:
    number = project_ref.removeprefix("P-")
    return f"O-{number}-{allocate(session, f'objective:{number}'):02d}"


def next_packet_ref(session: Session) -> str:
    return f"PP-{allocate(session, 'packet'):06d}"
