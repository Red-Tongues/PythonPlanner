"""Gate state for the atomic admission and claim (review R2, M2.1 fixes 1, 2 and 11).

**Gate revision (fix 2).** Every governance write that can change a genPacket's readiness bumps
``packets.gate_revision`` on the head of its lineage in the *same transaction*: waivers,
overrides, revocations, dependencies, and confirmations about the packet (created or decided).
Admission and claim read the revision before their readiness check and compare-and-set it in
their guarded UPDATE. A concurrent writer that commits in between changes the revision, so the
admission or claim matches no row and loses: no packet runs on a gate state that no longer holds.

**Context binding (fix 1).** ``packets.input_hash`` is the fingerprint of what the evaPackets
read: packet core plus the *head* project and objective. ``require_current_context`` recomputes
it; on a mismatch (e.g. the project was superseded) it refreshes the hash and sends the packet
back to ``evaluating_pre``, with events. The guarded UPDATE also requires the project and
objective heads it was checked against to still be heads.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from orchestrator.actors import Actor
from orchestrator.contracts import PacketStatus
from orchestrator.db.models import Confirmation, Objective, Packet, Project
from orchestrator.events import record_event

#: confirmation entity types that concern one genPacket's readiness
PACKET_CONFIRMATION_TYPES: frozenset[str] = frozenset({"packet", "packet_review", "failure_budget"})
#: statuses a context change sends back to evaluating_pre (allowed by the transition table)
REEVALUATE_FROM: frozenset[PacketStatus] = frozenset(
    {PacketStatus.READY, PacketStatus.QUEUED, PacketStatus.NEEDS_REVIEW}
)


def bump_gate(session: Session, packet: Packet, *, actor: Actor, cause: str) -> int | None:
    """Increment the gate revision of ``packet``'s lineage head (evented). Locked heads are
    terminal and need no gate; returns the new revision, or ``None`` when nothing changed."""
    from orchestrator.repository import get_packet

    head = get_packet(session, packet.ref)
    if head.locked:
        return None
    session.flush()  # pending ORM writes first, so statement order matches call order
    table = Packet.__table__
    session.connection().execute(
        update(table).where(table.c.id == head.id).values(gate_revision=table.c.gate_revision + 1)
    )
    new = int(
        session.connection()
        .execute(select(table.c.gate_revision).where(table.c.id == head.id))
        .scalar_one()
    )
    set_committed_value(head, "gate_revision", new)
    record_event(
        session,
        actor=actor,
        action="packet.gate_revision",
        entity_type="packet",
        entity_id=head.id,
        entity_ref=head.ref,
        entity_version=head.version,
        after={"gate_revision": new, "cause": cause},
        reason="readiness inputs changed",
    )
    session.flush()
    return new


def note_confirmation(session: Session, confirmation: Confirmation, *, actor: Actor) -> None:
    """A confirmation about a genPacket was created or decided: its gate changed."""
    if confirmation.entity_type not in PACKET_CONFIRMATION_TYPES:
        return
    packet = session.get(Packet, confirmation.entity_id)
    if packet is not None:
        bump_gate(session, packet, actor=actor, cause=f"confirmation {confirmation.status}")


def current_fingerprint(session: Session, packet: Packet) -> str:
    from orchestrator.evaluation.evaluator import packet_document

    return packet_document(session, packet)[1]


def require_current_context(session: Session, packet: Packet, *, actor: Actor) -> bool:
    """True when ``packet.input_hash`` still matches its current context. Otherwise refresh the
    hash, send the packet back to ``evaluating_pre`` (evented) and return False."""
    from orchestrator.evaluation.evaluator import refresh_input_hash
    from orchestrator.lifecycle import transition

    if packet.input_hash is not None and current_fingerprint(session, packet) == packet.input_hash:
        return True
    before = packet.input_hash
    refresh_input_hash(session, packet, actor)
    record_event(
        session,
        actor=actor,
        action="packet.context_changed",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"input_hash": before},
        after={"input_hash": packet.input_hash},
        reason="the packet's project or objective changed since it was evaluated",
    )
    if PacketStatus(packet.status) in REEVALUATE_FROM:
        transition(
            packet,
            PacketStatus.EVALUATING_PRE,
            actor,
            "context changed: re-evaluate",
            session=session,
        )
    session.flush()
    return False


def guarded_status_update(
    session: Session,
    packet: Packet,
    *,
    expected: PacketStatus,
    observed_gate: int,
    values: dict[str, Any],
) -> bool:
    """One compare-and-set UPDATE of a packet's status (fixes 2 and 11).

    It matches only while every condition the caller checked still holds: same row and version,
    expected status, same gate revision, same input hash, the packet, its project head and its
    objective head all still heads, and (for a claim) no live lease. Returns True if it won.
    """
    from orchestrator.evaluation.evaluator import head_context

    project, objective = head_context(session, packet)
    session.flush()
    table = Packet.__table__
    conditions = [
        table.c.id == packet.id,
        table.c.version == packet.version,
        table.c.status == expected,
        table.c.gate_revision == observed_gate,
        table.c.input_hash == packet.input_hash,
        ~select(Packet.id).where(Packet.supersedes_id == packet.id).exists(),
        ~select(Project.id).where(Project.supersedes_id == project.id).exists(),
    ]
    if objective is not None:
        conditions.append(
            ~select(Objective.id).where(Objective.supersedes_id == objective.id).exists()
        )
    if "claimed_by" in values:
        now = values["heartbeat_at"]
        conditions.append(table.c.claimed_by.is_(None) | (table.c.lease_expires_at < now))
    result = session.connection().execute(update(table).where(*conditions).values(**values))
    if result.rowcount != 1:
        return False
    for attr, value in values.items():
        set_committed_value(packet, attr, value)
    return True
