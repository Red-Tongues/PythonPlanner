"""Human-readable lineage refs: ``P-0001``, ``O-0001-01`` (objectives), ``PP-000001`` (packets).

A ref names a lineage; every version of the record keeps it (``(ref, version)`` is unique).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.db.models import Objective, Packet, Project


def _max_number(session: Session, column, prefix: str) -> int:  # type: ignore[no-untyped-def]
    # Refs are zero-padded, so the lexical maximum is the numeric maximum.
    current = session.scalar(select(func.max(column)).where(column.like(f"{prefix}%")))
    return 0 if current is None else int(str(current).removeprefix(prefix))


def next_project_ref(session: Session) -> str:
    return f"P-{_max_number(session, Project.ref, 'P-') + 1:04d}"


def next_objective_ref(session: Session, project_ref: str) -> str:
    prefix = f"O-{project_ref.removeprefix('P-')}-"
    return f"{prefix}{_max_number(session, Objective.ref, prefix) + 1:02d}"


def next_packet_ref(session: Session) -> str:
    return f"PP-{_max_number(session, Packet.ref, 'PP-') + 1:06d}"
