"""Persist a validated ``ProjectTree``: draft packets, events and (per scope) a confirmation."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import (
    AutonomyMode,
    PacketKind,
    PacketStatus,
    ProjectTree,
    Stage,
    response_structure_id,
)
from orchestrator.db.models import AutonomyScope, Confirmation, Objective, Packet, Project, new_id
from orchestrator.db.refs import next_objective_ref, next_packet_ref, next_project_ref
from orchestrator.events import record_event
from orchestrator.ingest.parser import name_from_path, parse_text
from orchestrator.ingest.readers import read_document
from orchestrator.llm.base import LLMProvider

log = logging.getLogger(__name__)

DETERMINISTIC_SOURCE = "deterministic_parser"


@dataclass(frozen=True)
class IngestResult:
    project: Project
    objectives: list[Objective]
    packets: list[Packet]
    confirmation: Confirmation | None


def ingestion_mode(session: Session) -> AutonomyMode:
    scope = session.get(AutonomyScope, Stage.INGESTION)
    # Missing scope (unseeded DB) → the safe default from PROJECT_PLAN §11.
    return AutonomyMode(scope.mode) if scope else AutonomyMode.CONFIRM


def persist_tree(
    session: Session,
    tree: ProjectTree,
    *,
    actor: Actor,
    source: str = DETERMINISTIC_SOURCE,
    source_path: str | None = None,
    source_sha256: str | None = None,
) -> IngestResult:
    """Write the tree as a new project with ``draft`` packets. Every row gets an event.

    Ids are assigned up front and each event is recorded before its row is flushed, so every
    flush carries the events that cover it (R1 fix 7).
    """
    core = tree.project
    project = Project(
        id=new_id(),
        ref=next_project_ref(session),
        name=core.name,
        brief=core.brief,
        industry=core.industry,
        sectors=list(core.sectors),
        core_values=list(core.values),
        goals=list(core.goals),
        scope=core.scope,
        reference_list=list(core.references),
        source=source,
        source_path=source_path,
        source_sha256=source_sha256,
        tree_contract=ProjectTree.contract_id(),
    )
    session.add(project)
    record_event(
        session,
        actor=actor,
        action="project.created",
        entity_type="project",
        entity_id=project.id,
        entity_ref=project.ref,
        entity_version=project.version,
        after={"name": project.name, "source": source, "source_path": source_path},
        reason="ingestion",
    )

    objectives: list[Objective] = []
    packets: list[Packet] = []
    for position, node in enumerate(tree.objectives, start=1):
        objective = Objective(
            id=new_id(),
            ref=next_objective_ref(session, project.ref),
            project_id=project.id,
            position=position,
            name=node.name,
            brief=node.brief,
            source=source,
        )
        session.add(objective)
        objectives.append(objective)
        record_event(
            session,
            actor=actor,
            action="objective.created",
            entity_type="objective",
            entity_id=objective.id,
            entity_ref=objective.ref,
            entity_version=1,
            after={"name": objective.name, "project": project.ref},
            reason="ingestion",
        )
        for task_position, task in enumerate(node.tasks, start=1):
            packet_id = new_id()
            packet = Packet(
                id=packet_id,
                ref=next_packet_ref(session),
                project_id=project.id,
                objective_id=objective.id,
                position=task_position,
                packet_kind=PacketKind.TASK,
                title=task.title,
                prompt_description=task.prompt_description,
                response_structure=task.response_structure,
                response_contract=response_structure_id(task.response_structure),
                repo_target_path=task.repo_target_path,
                status=PacketStatus.DRAFT,
                root_id=packet_id,  # a trunk packet is the root of its own branch tree
                depth=0,
                source=source,
            )
            session.add(packet)
            packets.append(packet)
            record_event(
                session,
                actor=actor,
                action="packet.created",
                entity_type="packet",
                entity_id=packet.id,
                entity_ref=packet.ref,
                entity_version=1,
                after={
                    "title": packet.title,
                    "status": PacketStatus.DRAFT.value,
                    "objective": objective.ref,
                },
                reason="ingestion",
            )

    confirmation = None
    mode = ingestion_mode(session)
    if mode is AutonomyMode.CONFIRM:
        confirmation = Confirmation(
            id=new_id(),
            stage=Stage.INGESTION,
            entity_type="project",
            entity_id=project.id,
            entity_ref=project.ref,
            entity_version=project.version,
            summary=(
                f"Approve the extracted tree for {project.ref} '{project.name}': "
                f"{len(objectives)} objectives, {len(packets)} tasks"
            ),
            payload={"contract": ProjectTree.contract_id(), "tree": tree.model_dump(mode="json")},
        )
        session.add(confirmation)
        record_event(
            session,
            actor=actor,
            action="confirmation.created",
            entity_type="confirmation",
            entity_id=confirmation.id,
            entity_ref=project.ref,
            after={"stage": Stage.INGESTION.value, "status": "pending"},
            reason="ingestion autonomy scope is 'confirm'",
        )

    record_event(
        session,
        actor=actor,
        action="ingest.completed",
        entity_type="project",
        entity_id=project.id,
        entity_ref=project.ref,
        entity_version=project.version,
        after={
            "objectives": len(objectives),
            "packets": len(packets),
            "autonomy_mode": mode.value,
            "contract": ProjectTree.contract_id(),
        },
        reason=f"ingested from {source_path or 'text'}",
    )
    session.flush()
    log.info("ingested %s: %d objectives, %d packets", project.ref, len(objectives), len(packets))
    return IngestResult(project, objectives, packets, confirmation)


def ingest_file(
    session: Session,
    path: str | Path,
    *,
    actor: Actor,
    project_name: str | None = None,
    provider: LLMProvider | None = None,
) -> IngestResult:
    """Read, parse, validate and persist a planning document.

    With ``provider`` the tree is extracted by an LLM (M2 §6) instead of the deterministic
    parser; both produce the same ``ProjectTree`` contract and the same confirmation flow.
    """
    path = Path(path)
    data = path.read_bytes() if path.is_file() else b""
    text = read_document(path)
    if provider is not None:
        from orchestrator.ingest.llm_extract import LLM_SOURCE, extract_tree_with_llm

        tree = extract_tree_with_llm(text, provider, name=project_name or name_from_path(path))
        if project_name:
            tree = tree.model_copy(
                update={"project": tree.project.model_copy(update={"name": project_name})}
            )
        source = LLM_SOURCE
    else:
        tree = parse_text(text, fallback_name=name_from_path(path), project_name=project_name)
        source = DETERMINISTIC_SOURCE
    return persist_tree(
        session,
        tree,
        actor=actor,
        source=source,
        source_path=str(path),
        source_sha256=hashlib.sha256(data).hexdigest(),
    )
