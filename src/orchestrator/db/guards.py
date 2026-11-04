"""Application-level guards on every flush (first line of defence; DB triggers are the guarantee).

``GuardedSession`` runs these checks before anything reaches the database:

1. **Immutability** (Requirement F, R1 fixes 1–2): no UPDATE/DELETE of a locked project, objective
   or packet, of a *superseded* one (it has a successor), of an accepted artifact, or of any row of
   an append-only table; no DELETE of a superseding version ("archive instead").
2. **Evented writes only** (R1 fix 7): every inserted, updated or deleted domain row must be
   covered by a new ``Event`` in the same flush — one naming that row's primary key, or a batch
   event for the entity type (``entity_id`` NULL). Settings and field definitions must also bump
   their ``version`` when they change. ORM bulk ``update()``/``delete()`` on domain models is
   rejected, because it would bypass both checks.

The governed write API (``repository``, ``lifecycle``, ``ingest``, field sync) records its event
*before* flushing, so it always passes; ad-hoc ORM edits fail loudly instead of going unaudited.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import ORMExecuteState, Session

from orchestrator.db.models import (
    Artifact,
    AutonomyScope,
    CandidateFieldDefinitionRow,
    ChannelRun,
    Confirmation,
    ControlState,
    EvaluationPass,
    Event,
    Exemplar,
    FieldRegistryRevision,
    Objective,
    Packet,
    Project,
    QualityReport,
    Setting,
    Threshold,
)
from orchestrator.errors import ImmutableRecordError, UnauditedWriteError

APPEND_ONLY_MODELS: tuple[type, ...] = (EvaluationPass, Event, FieldRegistryRevision)
LOCKABLE_MODELS: tuple[type, ...] = (Project, Objective, Packet)
VERSIONED_MODELS: tuple[type, ...] = (Setting, CandidateFieldDefinitionRow)

#: Domain models whose every write must be evented → the ``entity_type`` used in events.
#: ``EvaluationPass`` and ``Event`` are themselves append-only records and are exempt.
ENTITY_TYPES: dict[type, str] = {
    Project: "project",
    Objective: "objective",
    Packet: "packet",
    Setting: "setting",
    Threshold: "threshold",
    AutonomyScope: "autonomy_scope",
    CandidateFieldDefinitionRow: "candidate_field_definition",
    FieldRegistryRevision: "field_registry_revision",
    Confirmation: "confirmation",
    ControlState: "control_state",
    Artifact: "artifact",
    Exemplar: "exemplar",
    ChannelRun: "channel_run",
    QualityReport: "quality_report",
}


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


def has_successor(session: Session, obj: Any) -> bool:
    model = type(obj)
    with session.no_autoflush:
        stmt = select(model.id).where(model.supersedes_id == obj.id).limit(1)
        return session.execute(stmt).first() is not None


def assert_mutable(obj: Any, session: Session | None = None) -> None:
    """Raise ``ImmutableRecordError`` if ``obj`` (as stored) may not be changed or deleted."""
    if isinstance(obj, APPEND_ONLY_MODELS):
        raise ImmutableRecordError(f"{describe(obj)} is append-only")
    if isinstance(obj, LOCKABLE_MODELS):
        if _committed_value(obj, "locked"):
            raise ImmutableRecordError(
                f"{describe(obj)} is locked (completed work is immutable); "
                "create a new version with supersede_*() instead"
            )
        if session is not None and has_successor(session, obj):
            raise ImmutableRecordError(f"{describe(obj)} is superseded; history is never edited")
    if isinstance(obj, Artifact) and _committed_value(obj, "accepted"):
        raise ImmutableRecordError(f"{describe(obj)} is accepted and immutable")


def assert_deletable(obj: Any, session: Session | None = None) -> None:
    assert_mutable(obj, session)
    if isinstance(obj, LOCKABLE_MODELS) and _committed_value(obj, "supersedes_id") is not None:
        raise ImmutableRecordError(
            f"{describe(obj)} is a superseding version and cannot be deleted; archive instead"
        )


def primary_key(obj: Any) -> str | None:
    values = [getattr(obj, col.key) for col in inspect(type(obj)).primary_key]
    if len(values) != 1 or values[0] is None:
        return None
    value = values[0]
    return str(getattr(value, "value", value))


def _assert_evented(session: Session, objects: list[Any]) -> None:
    covered = {(e.entity_type, e.entity_id) for e in session.new if isinstance(e, Event)}
    for obj in objects:
        entity_type = ENTITY_TYPES.get(type(obj))
        if entity_type is None:
            continue
        if (entity_type, None) in covered or (entity_type, primary_key(obj)) in covered:
            continue
        raise UnauditedWriteError(
            f"{describe(obj)} changed without an event; use the governed write API "
            "(repository, lifecycle, ingest) so every state change is recorded"
        )


def _assert_version_bumped(obj: Any) -> None:
    if isinstance(obj, VERSIONED_MODELS) and not inspect(obj).attrs["version"].history.added:
        raise UnauditedWriteError(f"{describe(obj)} changed without bumping its version")


def _before_flush(session: Session, _context: Any, _instances: Any) -> None:
    deleted = [o for o in session.deleted if inspect(o).persistent]
    dirty = [o for o in session.dirty if session.is_modified(o, include_collections=False)]
    for obj in deleted:
        assert_deletable(obj, session)
    for obj in dirty:
        assert_mutable(obj, session)
        _assert_version_bumped(obj)
    _assert_evented(session, [*session.new, *dirty, *deleted])


def _do_orm_execute(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    model = mapper.class_ if mapper is not None else None
    if model in ENTITY_TYPES or model in APPEND_ONLY_MODELS:
        raise UnauditedWriteError(
            f"bulk ORM {'update' if state.is_update else 'delete'} on {model.__name__} would "
            "bypass the immutability and audit guards; use the governed write API"
        )


class GuardedSession(Session):
    """The kernel's session class: every flush passes the guards above."""


# Registered once on the class (not per sessionmaker), so no factory can end up unguarded.
event.listen(GuardedSession, "before_flush", _before_flush)
event.listen(GuardedSession, "do_orm_execute", _do_orm_execute)
