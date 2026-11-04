"""Application-level immutability guard (first line of defence; the DB triggers are the guarantee).

A ``before_flush`` hook rejects, with ``ImmutableRecordError``, any ORM flush that would update or
delete a locked packet, an accepted artifact, or any row of an append-only table. Repository
functions also check explicitly before touching a row, so callers get a clear error early.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from orchestrator.db.models import Artifact, EvaluationPass, Event, Objective, Packet, Project
from orchestrator.errors import ImmutableRecordError

APPEND_ONLY_MODELS: tuple[type, ...] = (EvaluationPass, Event)
LOCKABLE_MODELS: tuple[type, ...] = (Project, Objective, Packet)


def _committed_value(obj: Any, attr: str) -> Any:
    """The value of ``attr`` as last loaded from the database (before pending changes)."""
    history = inspect(obj).attrs[attr].history
    if history.deleted:
        return history.deleted[0]
    if history.unchanged:
        return history.unchanged[0]
    return getattr(obj, attr)


def describe(obj: Any) -> str:
    ref = getattr(obj, "ref", None) or getattr(obj, "id", "?")
    version = getattr(obj, "version", None)
    return f"{type(obj).__name__} {ref}" + (f" v{version}" if version else "")


def assert_mutable(obj: Any) -> None:
    """Raise ``ImmutableRecordError`` if ``obj`` (as stored) may not be changed or deleted."""
    if isinstance(obj, APPEND_ONLY_MODELS):
        raise ImmutableRecordError(f"{describe(obj)} is append-only")
    if isinstance(obj, LOCKABLE_MODELS) and _committed_value(obj, "locked"):
        raise ImmutableRecordError(
            f"{describe(obj)} is locked (completed work is immutable); "
            "create a new version with supersede_*() instead"
        )
    if isinstance(obj, Artifact) and _committed_value(obj, "accepted"):
        raise ImmutableRecordError(f"{describe(obj)} is accepted and immutable")


def _before_flush(session: Session, _context: Any, _instances: Any) -> None:
    for obj in session.deleted:
        if inspect(obj).persistent:
            assert_mutable(obj)
    for obj in session.dirty:
        if session.is_modified(obj, include_collections=False):
            assert_mutable(obj)


class GuardedSession(Session):
    """The kernel's session class: every flush passes the immutability guard."""


# Registered once on the class (not per sessionmaker), so no factory can end up unguarded.
event.listen(GuardedSession, "before_flush", _before_flush)
