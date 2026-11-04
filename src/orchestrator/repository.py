"""Governed data access for the core tree.

Every write goes through here (or ``orchestrator.lifecycle``), which (1) refuses to touch locked,
superseded or accepted rows with ``ImmutableRecordError`` before the database is asked, and
(2) records an ``events`` row for each state change *before* flushing it (the flush guard rejects
unevented writes, R1 fix 7). Actors are typed (``orchestrator.actors.Actor``, R1 fix 11).

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

from orchestrator.actors import Actor
from orchestrator.contracts import (
    AutonomyMode,
    Channel,
    ObjectiveCore,
    PacketStatus,
    ProjectCore,
    Stage,
    TaskCore,
    response_structure_id,
)
from orchestrator.db.guards import assert_deletable, assert_mutable, describe
from orchestrator.db.models import (
    Artifact,
    AutonomyScope,
    ChannelRun,
    EvaluationPass,
    Objective,
    Packet,
    Project,
    QualityReport,
    Setting,
    new_id,
)
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
#: Fields evaPackets read: changing them invalidates the evaluation set (B9).
CORE_PACKET_FIELDS: frozenset[str] = frozenset({"title", "prompt_description"})
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


# --------------------------------------------------------------------------- writes


def update_packet(
    session: Session,
    packet: Packet,
    changes: dict[str, Any],
    *,
    actor: Actor,
    reason: str | None = None,
) -> Packet:
    """Edit an unlocked head packet in place. Locked/superseded → ``ImmutableRecordError``.

    Core fields (what evaPackets read) may change in place only while the packet is a draft;
    after evaluation has started an edit is a new version (B9: ``supersede_packet``).
    """
    assert_mutable(packet, session)
    cleaned = _validate_packet_changes(packet, changes)
    core_changed = sorted(
        f for f in CORE_PACKET_FIELDS & set(cleaned) if cleaned[f] != getattr(packet, f)
    )
    if core_changed and PacketStatus(packet.status) is not PacketStatus.DRAFT:
        raise ImmutableRecordError(
            f"{packet.ref} is {PacketStatus(packet.status).value}: editing {core_changed} "
            "creates a new version — use supersede_packet (B9)"
        )
    before = packet_snapshot(packet)
    for name, value in cleaned.items():
        setattr(packet, name, value)
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
    session.flush()
    return packet


def dependents_of(session: Session, packet: Packet) -> list[str]:
    """What still refers to ``packet`` (children, evaluations, artifacts, runs, reports)."""
    found: list[str] = []
    children = select(Packet.ref).where(
        Packet.id != packet.id, (Packet.parent_id == packet.id) | (Packet.root_id == packet.id)
    )
    found += [f"child packet {ref}" for ref in session.scalars(children)]
    for model, label in (
        (EvaluationPass, "evaluation passes"),
        (Artifact, "artifacts"),
        (ChannelRun, "channel runs"),
        (QualityReport, "quality reports"),
    ):
        if session.scalar(select(model.id).where(model.packet_id == packet.id).limit(1)):
            found.append(label)
    return found


def delete_packet(
    session: Session, packet: Packet, *, actor: Actor, reason: str | None = None
) -> None:
    """Delete an unlocked v1 head packet nothing depends on; anything else → archive instead."""
    assert_deletable(packet, session)
    dependents = dependents_of(session, packet)
    if dependents:
        raise ImmutableRecordError(
            f"{describe(packet)} has dependents ({', '.join(dependents)}); archive it instead"
        )
    before = packet_snapshot(packet)
    record_event(
        session,
        actor=actor,
        action="packet.deleted",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before=before,
        reason=reason,
    )
    session.delete(packet)
    session.flush()


def lock_record(
    session: Session, row: Project | Objective, *, actor: Actor, reason: str | None = None
) -> None:
    """Lock a project or objective version (packets lock through the lifecycle)."""
    if isinstance(row, Packet):
        raise TypeError("packets are locked by completing/archiving them (lifecycle.transition)")
    assert_mutable(row, session)
    row.locked = True
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
    session.flush()


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
    actor: Actor,
    reason: str | None = None,
) -> Packet:
    """Create a new version of a packet with ``changes`` applied. The old row is never edited.

    The new version keeps the ref, project, objective, ``parent_id``, ``root_id`` and depth, gets
    ``version + 1``, ``supersedes_id`` → old, and a fresh ``draft`` lifecycle.
    """
    old = get_packet(session, old_ref)
    _check_supersedable(session, old)
    cleaned = _validate_packet_changes(old, dict(changes or {}))

    # Carried governance fields, then the validated changes (which include a recomputed
    # response_contract when the response structure changes — R1 fix 8: one source per field).
    fields: dict[str, Any] = {f: getattr(old, f) for f in EDITABLE_PACKET_FIELDS}
    fields["response_contract"] = old.response_contract
    fields.update(cleaned)
    new = Packet(
        id=new_id(),
        ref=old.ref,
        version=old.version + 1,
        supersedes_id=old.id,
        project_id=old.project_id,
        objective_id=old.objective_id,
        position=old.position,
        packet_kind=old.packet_kind,
        parent_id=old.parent_id,
        root_id=old.root_id,
        depth=old.depth,
        status=PacketStatus.DRAFT,
        locked=False,
        source="supersede",
        **fields,
    )
    session.add(new)
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
    session.flush()
    return new


def supersede_project(
    session: Session,
    old_ref: str,
    changes: dict[str, Any] | None = None,
    *,
    actor: Actor,
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
        id=new_id(),
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
    session.flush()
    return new


def supersede_objective(
    session: Session,
    old_ref: str,
    changes: dict[str, Any] | None = None,
    *,
    actor: Actor,
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
        id=new_id(),
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
    session.flush()
    return new


def set_setting(
    session: Session, key: str, value: Any, *, actor: Actor, reason: str | None = None
) -> Setting:
    """Change a tunable. Bumps its ``version`` and writes an event."""
    row = session.get(Setting, key)
    if row is None:
        raise NotFoundError(f"no setting {key!r}")
    before = {"value": row.value, "version": row.version}
    row.value = value
    row.version += 1
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
    session.flush()
    return row


def set_autonomy_mode(
    session: Session,
    stage: Stage | str,
    mode: AutonomyMode | str,
    *,
    actor: Actor,
    reason: str | None = None,
) -> AutonomyScope:
    """Change a stage's autonomy mode. Fixed scopes raise ``ImmutableRecordError``."""
    if not actor.is_user:
        raise PermissionError("only a user may change autonomy scopes")
    scope = session.get(AutonomyScope, Stage(stage))
    if scope is None:
        raise NotFoundError(f"no autonomy scope for stage {stage!r}")
    if scope.is_fixed:
        raise ImmutableRecordError(f"the {scope.stage.value} scope is fixed at {scope.mode.value}")
    before = {"mode": AutonomyMode(scope.mode).value}
    scope.mode = AutonomyMode(mode)
    record_event(
        session,
        actor=actor,
        action="autonomy_scope.changed",
        entity_type="autonomy_scope",
        entity_id=scope.stage.value,
        before=before,
        after={"mode": scope.mode.value},
        reason=reason,
    )
    session.flush()
    return scope
