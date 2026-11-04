"""Task branching and consensus promotion (M2 §5, B5, PROJECT_PLAN §8).

**Branching.** ``v_branch_triggers`` lists the ``split`` rules that fire on a packet's current
consensus (default: ``task_parsability >= 7``). ``apply_branch_triggers``:

* skips trunks that may not branch: not ``ready``, ``content_eval`` (B5), already at
  ``branching.max_depth`` or with ``branching.max_descendants`` reached;
* is idempotent per (packet version, rule, rule version) through ``branch_applications``'
  unique key — applying twice never creates a second set of children;
* respects the ``branching`` autonomy scope: ``auto`` splits now, ``confirm`` queues one
  confirmation, ``manual`` only reports;
* asks the Local Generation model for a ``subtask_list`` (validated against ``SubtaskList``),
  creates the children (``parent_id``, ``root_id``, ``depth``, ``branch_reason``) as drafts and
  moves the trunk to ``waiting_for_children``. The trunk is never removed; it completes by
  deterministic roll-up when every child completes (``rollup_trunks``).

**Promotion.** ``promote_consensus`` turns accepted consensus into master governance fields per
the ``candidate_promotion`` scope: the routing suggestion (``channel``) from the fired route
rules, with ``require_approval`` rules sending the packet to the approval queue instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.config import get_settings
from orchestrator.contracts import (
    AutonomyMode,
    Channel,
    ConfirmationStatus,
    PacketKind,
    PacketStatus,
    Stage,
    SubtaskList,
    ThresholdAction,
    response_structure_id,
)
from orchestrator.db.models import (
    AutonomyScope,
    BranchApplication,
    Confirmation,
    Packet,
    Threshold,
    new_id,
)
from orchestrator.db.refs import next_packet_ref
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event
from orchestrator.gates import note_confirmation
from orchestrator.llm import LLMProvider
from orchestrator.settings_store import get_int

log = logging.getLogger(__name__)
BRANCHER = Actor.system("brancher")


class BranchingError(OrchestratorError):
    """The subtask list could not be obtained or validated."""


@dataclass
class BranchOutcome:
    packet_ref: str
    rule: str
    action: str  # split | confirmation | skipped | manual
    children: list[str] = field(default_factory=list)
    reason: str = ""


def children_of(session: Session, trunk: Packet) -> list[Packet]:
    """Head versions of the packets split from any version of ``trunk``'s lineage."""
    from orchestrator.repository import _heads

    lineage = select(Packet.id).where(Packet.ref == trunk.ref)
    stmt = _heads(Packet).where(Packet.parent_id.in_(lineage), Packet.ref != trunk.ref)
    return list(session.scalars(stmt.order_by(Packet.position, Packet.ref)))


def _mode(session: Session, stage: Stage) -> AutonomyMode:
    scope = session.get(AutonomyScope, stage)
    return AutonomyMode(scope.mode) if scope else AutonomyMode.CONFIRM


def _root_of(session: Session, packet: Packet) -> Packet:
    if packet.root_id and packet.root_id != packet.id:
        root = session.get(Packet, packet.root_id)
        if root is not None:
            return root
    return packet


def _descendants(session: Session, root: Packet) -> int:
    lineage = select(Packet.id).where(Packet.ref == root.ref)
    refs = session.scalars(
        select(Packet.ref).where(Packet.root_id.in_(lineage), Packet.ref != root.ref).distinct()
    ).all()
    return len(refs)


def fired_rules(session: Session, action: ThresholdAction | None = None) -> list[dict]:
    sql = "SELECT * FROM v_branch_triggers"
    params: dict[str, str] = {}
    if action is not None:
        sql += " WHERE action = :action"
        params["action"] = action.value
    return [
        dict(r)
        for r in session.execute(text(sql + " ORDER BY packet_ref, precedence"), params).mappings()
    ]


def subtasks_with_llm(
    session: Session, packet: Packet, provider: LLMProvider, *, max_attempts: int = 3
) -> SubtaskList:
    """A validated ``subtask_list`` from the Local Generation model (retries with corrections)."""
    from orchestrator.contracts.evaluation import concise_error, json_schema
    from orchestrator.evaluation.evaluator import packet_document
    from orchestrator.evaluation.normalise import OutputFormatError, extract_json
    from orchestrator.evaluation.prompts import correction_suffix, subtask_prompt

    document, _ = packet_document(session, packet)
    prompt = subtask_prompt(document, json_schema(SubtaskList))
    current, error = prompt, ""
    for _attempt in range(max_attempts):
        response = provider.complete(
            current,
            model=get_settings().model_c,
            max_tokens=1500,
            temperature=0.0,
            json_mode=True,
            schema=json_schema(SubtaskList),
        )
        try:
            return SubtaskList.model_validate(extract_json(response.text))
        except (OutputFormatError, ValidationError) as exc:
            error = concise_error(exc) if isinstance(exc, ValidationError) else str(exc)
            current = prompt + correction_suffix(error)
    raise BranchingError(
        f"{packet.ref}: no valid subtask_list after {max_attempts} attempts ({error})"
    )


def split_packet(
    session: Session,
    packet: Packet,
    rule: Threshold,
    provider: LLMProvider,
    *,
    actor: Actor = BRANCHER,
) -> list[Packet]:
    """Create the children of one split and park the trunk (idempotent per rule version)."""
    from orchestrator.lifecycle import transition

    existing = session.scalar(
        select(BranchApplication).where(
            BranchApplication.packet_id == packet.id,
            BranchApplication.packet_version == packet.version,
            BranchApplication.threshold_id == rule.id,
            BranchApplication.threshold_version == rule.version,
        )
    )
    if existing is not None:
        return [session.get(Packet, c) for c in existing.children]  # type: ignore[misc]
    subtasks = subtasks_with_llm(session, packet, provider)
    root = _root_of(session, packet)
    children: list[Packet] = []
    for position, task in enumerate(subtasks.subtasks, start=1):
        child = Packet(
            id=new_id(),
            ref=next_packet_ref(session),
            project_id=packet.project_id,
            objective_id=packet.objective_id,
            position=packet.position * 100 + position,
            packet_kind=PacketKind.TASK,
            title=task.title,
            prompt_description=task.prompt_description,
            response_structure=task.response_structure,
            response_contract=response_structure_id(task.response_structure),
            repo_target_path=task.repo_target_path,
            parent_id=packet.id,
            root_id=root.id,
            depth=packet.depth + 1,
            branch_reason=f"{rule.name} v{rule.version} on {packet.ref} v{packet.version}",
            status=PacketStatus.DRAFT,
            source="branch",
        )
        session.add(child)
        record_event(
            session,
            actor=actor,
            action="packet.branched",
            entity_type="packet",
            entity_id=child.id,
            entity_ref=child.ref,
            entity_version=1,
            after={
                "parent": packet.ref,
                "root": root.ref,
                "depth": child.depth,
                "title": child.title,
            },
            reason=child.branch_reason,
        )
        children.append(child)
    application = BranchApplication(
        id=new_id(),
        packet_id=packet.id,
        packet_version=packet.version,
        threshold_id=rule.id,
        threshold_version=rule.version,
        children=[c.id for c in children],
    )
    session.add(application)
    record_event(
        session,
        actor=actor,
        action="branch.applied",
        entity_type="branch_application",
        entity_id=application.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={
            "rule": rule.name,
            "rule_version": rule.version,
            "children": [c.ref for c in children],
        },
    )
    session.flush()
    # The split is a kernel move (D8 ready → waiting_for_children); an owner's approval only
    # authorises it, and is recorded as the approval and split events above.
    transition(
        packet,
        PacketStatus.WAITING_FOR_CHILDREN,
        actor if not actor.is_user else BRANCHER,
        f"split into {len(children)} children"
        + (f" (approved by {actor})" if actor.is_user else ""),
        session=session,
    )
    return children


def apply_branch_triggers(
    session: Session,
    provider: LLMProvider,
    *,
    actor: Actor = BRANCHER,
    approved: set[str] | None = None,
) -> list[BranchOutcome]:
    """Apply every fired ``split`` rule, within the B5 limits and the branching scope.

    ``approved`` holds packet ids a user has approved (from a branching confirmation).
    """
    max_depth = get_int(session, "branching.max_depth", 2)
    max_descendants = get_int(session, "branching.max_descendants", 12)
    mode = _mode(session, Stage.BRANCHING)
    outcomes: list[BranchOutcome] = []
    for fired in fired_rules(session, ThresholdAction.SPLIT):
        packet = session.get(Packet, fired["packet_id"])
        rule = session.get(Threshold, fired["threshold_id"])
        assert packet is not None and rule is not None
        outcome = BranchOutcome(packet.ref, rule.name, "skipped")
        outcomes.append(outcome)
        if PacketKind(packet.packet_kind) is PacketKind.CONTENT_EVAL:
            outcome.reason = "content_eval packets never branch (B5)"
            continue
        if PacketStatus(packet.status) is not PacketStatus.READY:
            outcome.reason = f"status is {PacketStatus(packet.status).value}"
            continue
        if packet.depth >= max_depth:
            outcome.reason = f"max_branch_depth {max_depth} reached"
            continue
        root = _root_of(session, packet)
        if _descendants(session, root) >= max_descendants:
            outcome.reason = f"descendant cap {max_descendants} reached for {root.ref}"
            continue
        rejected = session.scalar(
            select(Confirmation.id).where(
                Confirmation.stage == Stage.BRANCHING,
                Confirmation.entity_id == packet.id,
                Confirmation.status == ConfirmationStatus.REJECTED,
            )
        )
        if rejected is not None:
            outcome.reason = "the split was rejected for this version"
            continue
        if mode is AutonomyMode.MANUAL:
            outcome.action, outcome.reason = "manual", "branching scope is manual"
            continue
        if mode is AutonomyMode.CONFIRM and packet.id not in (approved or set()):
            outcome.action = "confirmation"
            outcome.reason = _request_confirmation(session, packet, rule, actor)
            continue
        outcome.action = "split"
        outcome.children = [
            c.ref for c in split_packet(session, packet, rule, provider, actor=actor)
        ]
    return outcomes


def _request_confirmation(session: Session, packet: Packet, rule: Threshold, actor: Actor) -> str:
    pending = session.scalar(
        select(Confirmation).where(
            Confirmation.stage == Stage.BRANCHING,
            Confirmation.entity_id == packet.id,
            Confirmation.status == ConfirmationStatus.PENDING,
        )
    )
    if pending is not None:
        return f"awaiting confirmation {pending.id}"
    confirmation = Confirmation(
        id=new_id(),
        stage=Stage.BRANCHING,
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        summary=f"Split {packet.ref} v{packet.version} '{packet.title}' ({rule.name})",
        payload={"rule": rule.name, "threshold_id": rule.id, "rule_version": rule.version},
    )
    session.add(confirmation)
    record_event(
        session,
        actor=actor,
        action="confirmation.created",
        entity_type="confirmation",
        entity_id=confirmation.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"stage": "branching", "rule": rule.name},
        reason="branching autonomy scope is 'confirm'",
    )
    session.flush()
    note_confirmation(session, confirmation, actor=actor)
    return f"awaiting confirmation {confirmation.id}"


def rollup_trunks(session: Session, *, actor: Actor = BRANCHER) -> list[str]:
    """Complete every waiting trunk whose children are all completed (deterministic roll-up)."""
    from orchestrator.lifecycle import transition
    from orchestrator.repository import _heads

    completed: list[str] = []
    for trunk in session.scalars(
        _heads(Packet).where(Packet.status == PacketStatus.WAITING_FOR_CHILDREN)
    ):
        children = children_of(session, trunk)
        if children and all(PacketStatus(c.status) is PacketStatus.COMPLETED for c in children):
            transition(
                trunk,
                PacketStatus.COMPLETED,
                actor,
                f"all {len(children)} children completed",
                session=session,
            )
            completed.append(trunk.ref)
    return completed


# --------------------------------------------------------------------------- promotion


ROUTES = {
    ThresholdAction.ROUTE_WORKBENCH_PRODUCTION: Channel.WORKBENCH_PRODUCTION,
    ThresholdAction.ROUTE_LOCAL_GENERATION: Channel.LOCAL_GENERATION,
}


def promote_consensus(
    session: Session, packet: Packet, *, actor: Actor = BRANCHER
) -> dict[str, object]:
    """Promote accepted consensus into master governance fields (``candidate_promotion``)."""
    rules = [r for r in fired_rules(session) if r["packet_id"] == packet.id]
    approval = [r["threshold_name"] for r in rules if r["action"] == "require_approval"]
    routes = sorted(
        (r for r in rules if ThresholdAction(r["action"]) in ROUTES),
        key=lambda r: (r["precedence"], r["threshold_name"]),
    )
    channel = ROUTES[ThresholdAction(routes[0]["action"])] if routes else Channel.LOCAL_GENERATION
    proposal: dict[str, object] = {
        "channel": channel.value,
        "rules": [r["threshold_name"] for r in routes],
    }
    if approval:
        proposal["approval_rules"] = approval
    mode = _mode(session, Stage.CANDIDATE_PROMOTION)
    if approval or mode is not AutonomyMode.AUTO:
        if mode is AutonomyMode.MANUAL and not approval:
            return {**proposal, "applied": False, "mode": "manual"}
        pending = session.scalar(
            select(Confirmation).where(
                Confirmation.stage == Stage.ROUTING,
                Confirmation.entity_id == packet.id,
                Confirmation.status == ConfirmationStatus.PENDING,
            )
        )
        if pending is None:
            confirmation = Confirmation(
                id=new_id(),
                stage=Stage.ROUTING,
                entity_type="packet",
                entity_id=packet.id,
                entity_ref=packet.ref,
                entity_version=packet.version,
                summary=f"Route {packet.ref} v{packet.version} to {channel.value}"
                + (f" (approval required by {', '.join(approval)})" if approval else ""),
                payload=proposal,
            )
            session.add(confirmation)
            record_event(
                session,
                actor=actor,
                action="confirmation.created",
                entity_type="confirmation",
                entity_id=confirmation.id,
                entity_ref=packet.ref,
                entity_version=packet.version,
                after=proposal,
                reason="approval rule fired" if approval else "promotion scope is 'confirm'",
            )
            session.flush()
            note_confirmation(session, confirmation, actor=actor)
        return {**proposal, "applied": False}
    apply_channel(session, packet, channel, actor=actor, reason="consensus promotion (auto)")
    return {**proposal, "applied": True}


def apply_channel(
    session: Session, packet: Packet, channel: Channel, *, actor: Actor, reason: str
) -> None:
    from orchestrator.repository import update_packet

    if packet.channel != channel:
        update_packet(session, packet, {"channel": channel}, actor=actor, reason=reason)
