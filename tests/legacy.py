"""Legacy ``supersede_*`` writers, kept for tests only.

M3a (R6 OQ21) retired whole-row versioning from the kernel: projects, objectives and developing
genPackets are edited in place. The schema still carries ``supersedes_id`` and its lineage
triggers for legacy rows, so database-level tests build such rows with these helpers (the
triggers are never disabled). No ``src/`` code path calls them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import ObjectiveCore, PacketStatus, ProjectCore
from orchestrator.db.guards import describe
from orchestrator.db.models import Objective, Packet, Project, new_id
from orchestrator.errors import AlreadySupersededError
from orchestrator.events import record_event
from orchestrator.repository import (
    EDITABLE_PACKET_FIELDS,
    OBJECTIVE_FIELDS,
    PROJECT_FIELDS,
    _reject_unknown,
    _validate_packet_changes,
    get_objective,
    get_packet,
    get_project,
    objective_snapshot,
    packet_snapshot,
    project_snapshot,
    successor_of,
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
        lineage_key=old.lineage_key,
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
    from orchestrator.gates import bump_scope  # noqa: F401

    # C25: interrupt / core supersession is a governance write for its whole scope
    bump_scope(session, "project", new.ref, actor=actor, cause="project superseded")
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
    from orchestrator.gates import bump_scope  # noqa: F401

    # C25: interrupt / core supersession is a governance write for its whole scope
    bump_scope(session, "objective", new.ref, actor=actor, cause="objective superseded")
    return new
