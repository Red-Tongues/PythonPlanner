"""Governed data access for the core tree.

Every write goes through here (or ``orchestrator.lifecycle``), which (1) refuses to touch locked,
superseded or accepted rows with ``ImmutableRecordError`` before the database is asked, and
(2) writes an ``events`` row for each state change.

Versioning (A2): a ref names a lineage. ``get_packet(s, "PP-000001")`` returns the current head;
``"PP-000001@1"`` addresses a specific version. ``supersede_*`` inserts a new version with the
same ref, ``version + 1`` and ``supersedes_id`` → predecessor. It never edits the old row and
never re-points children or ``parent_id`` / ``root_id``; children belong to the *lineage* of
their parent, and the head is derived (``v_*_heads`` views / ``_heads()`` here).
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from orchestrator.contracts import (
    Channel,
    ObjectiveCore,
    PacketStatus,
    ProjectCore,
    TaskCore,
    response_structure_id,
)
from orchestrator.db.guards import assert_mutable, describe
from orchestrator.db.models import Objective, Packet, Project, Setting
from orchestrator.errors import AlreadySupersededError, ImmutableRecordError, NotFoundError
from orchestrator.events import record_event

M = TypeVar("M", Project, Objective, Packet)

#: Fields a caller may change on an unlocked packet head / carry into a new version.
EDITABLE_PACKET_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "prompt_description",
        "response_structure",
        "channel",
        "priority_level",
        "manual_boost",
        "repo_target_path",
        "branch_reason",
    }
)
#: ORM column → ProjectCore field.
PROJECT_FIELDS: dict[str, str] = {
    "name": "name",
    "brief": "brief",
    "industry": "industry",
    "sectors": "sectors",
    "core_values": "values",
    "goals": "goals",
    "scope": "scope",
    "reference_list": "references",
}
OBJECTIVE_FIELDS: frozenset[str] = frozenset({"name", "brief", "priority_level"})


# --------------------------------------------------------------------------- refs and heads


def parse_ref(ref: str) -> tuple[str, int | None]:
    """``"PP-000001"`` → (ref, None); ``"PP-000001@2"`` → (ref, 2)."""
    base, sep, version = ref.strip().partition("@")
    if not sep:
        return base, None
    if not version.isdigit():
        raise NotFoundError(f"bad version in ref {ref!r} (expected REF@N)")
    return base, int(version)


def _heads(model: type[M]) -> Select[tuple[M]]:
    """Rows of ``model`` that no other row supersedes."""
    superseded = select(model.supersedes_id).where(model.supersedes_id.is_not(None))
    return select(model).where(model.id.not_in(superseded))


def _get(session: Session, model: type[M], ref: str) -> M:
    base, version = parse_ref(ref)
    stmt = _heads(model) if version is None else select(model).where(model.version == version)
    row = session.scalar(stmt.where(model.ref == base))
    if row is None:
        raise NotFoundError(f"no {model.__tablename__[:-1]} with ref {ref!r}")
    return row


def get_project(session: Session, ref: str) -> Project:
    return _get(session, Project, ref)


def get_objective(session: Session, ref: str) -> Objective:
    return _get(session, Objective, ref)


def get_packet(session: Session, ref: str) -> Packet:
    return _get(session, Packet, ref)


def successor_of(session: Session, row: M) -> M | None:
    model = type(row)
    return session.scalar(select(model).where(model.supersedes_id == row.id))


def is_head(session: Session, row: Project | Objective | Packet) -> bool:
    return successor_of(session, row) is None


def versions_of(session: Session, model: type[M], ref: str) -> list[M]:
    return list(session.scalars(select(model).where(model.ref == ref).order_by(model.version)))


def list_projects(session: Session) -> list[Project]:
    return list(session.scalars(_heads(Project).order_by(Project.ref)))


def objectives_of(session: Session, project: Project) -> list[Objective]:
    """Head objectives belonging to any version of ``project``'s lineage."""
    lineage = select(Project.id).where(Project.ref == project.ref)
    stmt = _heads(Objective).where(Objective.project_id.in_(lineage))
    return list(session.scalars(stmt.order_by(Objective.position, Objective.ref)))


def packets_of(session: Session, objective: Objective) -> list[Packet]:
    """Head packets belonging to any version of ``objective``'s lineage."""
    lineage = select(Objective.id).where(Objective.ref == objective.ref)
    stmt = _heads(Packet).where(Packet.objective_id.in_(lineage))
    return list(session.scalars(stmt.order_by(Packet.position, Packet.ref)))


# --------------------------------------------------------------------------- snapshots


def packet_snapshot(packet: Packet) -> dict[str, Any]:
    """A compact, JSON-safe summary used in event before/after fields."""
    return {
        "ref": packet.ref,
        "version": packet.version,
        "title": packet.title,
        "status": PacketStatus(packet.status).value,
        "locked": bool(packet.locked),
        "response_structure": packet.response_structure,
        "channel": Channel(packet.channel).value if packet.channel else None,
        "priority_level": packet.priority_level,
        "repo_target_path": packet.repo_target_path,
    }


def project_snapshot(project: Project) -> dict[str, Any]:
    return {
        "ref": project.ref,
        "version": project.version,
        "locked": bool(project.locked),
        **{c: getattr(project, c) for c in PROJECT_FIELDS},
    }


def objective_snapshot(objective: Objective) -> dict[str, Any]:
    return {
        "ref": objective.ref,
        "version": objective.version,
        "locked": bool(objective.locked),
        **{c: getattr(objective, c) for c in sorted(OBJECTIVE_FIELDS)},
    }


# --------------------------------------------------------------------------- validation


def _reject_unknown(changes: dict[str, Any], allowed: frozenset[str] | set[str], what: str) -> None:
    unknown = set(changes) - set(allowed)
    if unknown:
        raise ValueError(
            f"cannot change {sorted(unknown)} on a {what}; editable: {sorted(allowed)}"
        )


def _validate_packet_changes(packet: Packet, changes: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(changes, EDITABLE_PACKET_FIELDS, "packet (status goes through lifecycle)")
    merged = {
        "title": changes.get("title", packet.title),
        "prompt_description": changes.get("prompt_description", packet.prompt_description),
        "response_structure": changes.get("response_structure", packet.response_structure),
        "repo_target_path": changes.get("repo_target_path", packet.repo_target_path),
    }
    TaskCore.model_validate(merged)  # same contract as ingestion
    cleaned = dict(changes)
    if cleaned.get("channel") is not None:
        cleaned["channel"] = Channel(cleaned["channel"])
    if "response_structure" in cleaned:
        cleaned["response_contract"] = response_structure_id(cleaned["response_structure"])
    return cleaned


def _assert_head(session: Session, row: Project | Objective | Packet) -> None:
    newer = successor_of(session, row)
    if newer is not None:
        raise ImmutableRecordError(
            f"{describe(row)} is superseded by v{newer.version}; history is never edited"
        )


# --------------------------------------------------------------------------- writes


def update_packet(
    session: Session,
    packet: Packet,
    changes: dict[str, Any],
    *,
    actor: str,
    reason: str | None = None,
) -> Packet:
    """Edit an unlocked head packet in place. Locked/superseded → ``ImmutableRecordError``."""
    assert_mutable(packet)
    _assert_head(session, packet)
    cleaned = _validate_packet_changes(packet, changes)
    before = packet_snapshot(packet)
    for name, value in cleaned.items():
        setattr(packet, name, value)
    session.flush()
    record_event(
        session,
        actor=actor,
        action="packet.updated",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before=before,
        after=packet_snapshot(packet),
        reason=reason,
    )
    return packet


def delete_packet(
    session: Session, packet: Packet, *, actor: str, reason: str | None = None
) -> None:
    """Delete an unlocked head packet. Locked/superseded → ``ImmutableRecordError``."""
    assert_mutable(packet)
    _assert_head(session, packet)
    before = packet_snapshot(packet)
    packet_id, ref, version = packet.id, packet.ref, packet.version
    session.delete(packet)
    session.flush()
    record_event(
        session,
        actor=actor,
        action="packet.deleted",
        entity_type="packet",
        entity_id=packet_id,
        entity_ref=ref,
        entity_version=version,
        before=before,
        reason=reason,
    )


def lock_record(
    session: Session, row: Project | Objective, *, actor: str, reason: str | None = None
) -> None:
    """Lock a project or objective version (packets lock through the lifecycle)."""
    if isinstance(row, Packet):
        raise TypeError("packets are locked by completing/archiving them (lifecycle.transition)")
    assert_mutable(row)
    row.locked = True
    session.flush()
    record_event(
        session,
        actor=actor,
        action=f"{row.__tablename__[:-1]}.locked",
        entity_type=row.__tablename__[:-1],
        entity_id=row.id,
        entity_ref=row.ref,
        entity_version=row.version,
        after={"locked": True},
        reason=reason,
    )


def _check_supersedable(session: Session, old: Project | Objective | Packet) -> None:
    newer = successor_of(session, old)
    if newer is not None:
        raise AlreadySupersededError(
            f"{describe(old)} is already superseded by v{newer.version}; "
            "supersede the latest version"
        )


def supersede_packet(
    session: Session,
    old_ref: str,
    changes: dict[str, Any] | None = None,
    *,
    actor: str = "user",
    reason: str | None = None,
) -> Packet:
    """Create a new version of a packet with ``changes`` applied. The old row is never edited.

    The new version keeps the ref, project, objective, ``parent_id``, ``root_id`` and depth, gets
    ``version + 1``, ``supersedes_id`` → old, and a fresh ``draft`` lifecycle.
    """
    old = get_packet(session, old_ref)
    _check_supersedable(session, old)
    cleaned = _validate_packet_changes(old, dict(changes or {}))

    carried = {f: getattr(old, f) for f in EDITABLE_PACKET_FIELDS}
    new = Packet(
        ref=old.ref,
        version=old.version + 1,
        supersedes_id=old.id,
        project_id=old.project_id,
        objective_id=old.objective_id,
        position=old.position,
        packet_kind=old.packet_kind,
        response_contract=old.response_contract,
        parent_id=old.parent_id,
        root_id=old.root_id,
        depth=old.depth,
        status=PacketStatus.DRAFT,
        locked=False,
        source="supersede",
        **{**carried, **cleaned},
    )
    session.add(new)
    session.flush()
    record_event(
        session,
        actor=actor,
        action="packet.superseded",
        entity_type="packet",
        entity_id=new.id,
        entity_ref=new.ref,
        entity_version=new.version,
        before=packet_snapshot(old),
        after={**packet_snapshot(new), "changes": sorted(changes or {})},
        reason=reason,
    )
    return new


def supersede_project(
    session: Session,
    old_ref: str,
    changes: dict[str, Any] | None = None,
    *,
    actor: str = "user",
    reason: str | None = None,
) -> Project:
    """New version of a project's core fields. Objectives stay attached to the lineage."""
    old = get_project(session, old_ref)
    _check_supersedable(session, old)
    changes = dict(changes or {})
    _reject_unknown(changes, set(PROJECT_FIELDS), "project")
    merged = {c: changes.get(c, getattr(old, c)) for c in PROJECT_FIELDS}
    ProjectCore.model_validate({PROJECT_FIELDS[c]: v for c, v in merged.items()})

    new = Project(
        ref=old.ref,
        version=old.version + 1,
        supersedes_id=old.id,
        source="supersede",
        source_path=old.source_path,
        source_sha256=old.source_sha256,
        tree_contract=old.tree_contract,
        **merged,
    )
    session.add(new)
    session.flush()
    record_event(
        session,
        actor=actor,
        action="project.superseded",
        entity_type="project",
        entity_id=new.id,
        entity_ref=new.ref,
        entity_version=new.version,
        before=project_snapshot(old),
        after={**project_snapshot(new), "changes": sorted(changes)},
        reason=reason,
    )
    return new


def supersede_objective(
    session: Session,
    old_ref: str,
    changes: dict[str, Any] | None = None,
    *,
    actor: str = "user",
    reason: str | None = None,
) -> Objective:
    """New version of an objective's core fields. Packets stay attached to the lineage."""
    old = get_objective(session, old_ref)
    _check_supersedable(session, old)
    changes = dict(changes or {})
    _reject_unknown(changes, OBJECTIVE_FIELDS, "objective")
    merged = {c: changes.get(c, getattr(old, c)) for c in OBJECTIVE_FIELDS}
    ObjectiveCore.model_validate({"name": merged["name"], "brief": merged["brief"]})

    new = Objective(
        ref=old.ref,
        version=old.version + 1,
        supersedes_id=old.id,
        project_id=old.project_id,
        position=old.position,
        tags=dict(old.tags),
        scores=dict(old.scores),
        source="supersede",
        **merged,
    )
    session.add(new)
    session.flush()
    record_event(
        session,
        actor=actor,
        action="objective.superseded",
        entity_type="objective",
        entity_id=new.id,
        entity_ref=new.ref,
        entity_version=new.version,
        before=objective_snapshot(old),
        after={**objective_snapshot(new), "changes": sorted(changes)},
        reason=reason,
    )
    return new


def set_setting(
    session: Session, key: str, value: Any, *, actor: str, reason: str | None = None
) -> Setting:
    """Change a tunable. Bumps its ``version`` and writes an event."""
    row = session.get(Setting, key)
    if row is None:
        raise NotFoundError(f"no setting {key!r}")
    before = {"value": row.value, "version": row.version}
    row.value = value
    row.version += 1
    session.flush()
    record_event(
        session,
        actor=actor,
        action="setting.changed",
        entity_type="setting",
        entity_id=key,
        entity_version=row.version,
        before=before,
        after={"key": key, "value": value, "version": row.version},
        reason=reason,
    )
    return row
