"""Packet lifecycle: an explicit allowed-transition table, applied with an event per change.

System and LLM actors may only follow ``ALLOWED_TRANSITIONS``. A *user* actor (typed, R1 fix 11 —
see ``orchestrator.actors``) may additionally complete or archive any non-terminal packet by hand:
the ``manual`` autonomy mode means "the user does it", so a person must be able to close work the
kernel never touched. Completing or archiving locks the packet in the same UPDATE statement.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, object_session

from orchestrator.actors import Actor
from orchestrator.contracts import LOCKING_STATUSES, PacketStatus
from orchestrator.db.guards import assert_mutable
from orchestrator.db.models import Event, Packet, utcnow
from orchestrator.errors import ImmutableRecordError, InvalidTransitionError
from orchestrator.events import record_event
from orchestrator.repository import is_head, packet_snapshot

S = PacketStatus

#: The B9 genPacket lifecycle. Side states: needs_review (a human must look), held / blocked
#: (human restrictions), awaiting_confirmation (an autonomy-scope approval), waiting_for_children
#: (a split trunk, B5), failed. Legacy M1 statuses only have exits into this lifecycle.
_RELEASE = frozenset({S.DRAFT, S.EVALUATING_PRE, S.READY, S.ARCHIVED})
ALLOWED_TRANSITIONS: dict[PacketStatus, frozenset[PacketStatus]] = {
    S.DRAFT: frozenset({S.EVALUATING_PRE, S.HELD, S.BLOCKED, S.ARCHIVED}),
    S.EVALUATING_PRE: frozenset(
        {S.READY, S.NEEDS_REVIEW, S.FAILED, S.HELD, S.BLOCKED, S.DRAFT, S.WAITING_FOR_CHILDREN}
    ),
    S.READY: frozenset(
        {
            S.QUEUED,
            S.EVALUATING_PRE,
            S.NEEDS_REVIEW,
            S.AWAITING_CONFIRMATION,
            S.WAITING_FOR_CHILDREN,
            S.HELD,
            S.BLOCKED,
            S.ARCHIVED,
        }
    ),
    S.QUEUED: frozenset({S.RUNNING, S.READY, S.EVALUATING_PRE, S.HELD, S.BLOCKED}),
    S.RUNNING: frozenset({S.EVALUATING_POST, S.READY, S.FAILED, S.AWAITING_CONFIRMATION}),
    S.EVALUATING_POST: frozenset({S.COMPLETED, S.NEEDS_REVIEW, S.FAILED, S.READY}),
    S.NEEDS_REVIEW: frozenset({S.EVALUATING_PRE, S.READY, S.HELD, S.BLOCKED, S.ARCHIVED}),
    S.HELD: _RELEASE,
    S.BLOCKED: _RELEASE,
    S.AWAITING_CONFIRMATION: frozenset({S.READY, S.EVALUATING_PRE, S.NEEDS_REVIEW, S.FAILED}),
    S.WAITING_FOR_CHILDREN: frozenset({S.COMPLETED, S.NEEDS_REVIEW, S.FAILED}),
    S.FAILED: frozenset({S.DRAFT, S.EVALUATING_PRE, S.AWAITING_CONFIRMATION, S.ARCHIVED}),
    S.COMPLETED: frozenset(),
    S.ARCHIVED: frozenset(),
    # legacy M1 statuses
    S.EVALUATING: frozenset({S.EVALUATING_PRE, S.READY, S.FAILED, S.ARCHIVED}),
    S.IN_PROGRESS: frozenset({S.RUNNING, S.FAILED, S.ARCHIVED}),
    S.PROCESSED: frozenset({S.EVALUATING_POST, S.ARCHIVED}),
}

#: Extra transitions only a user actor may make, from any non-terminal status.
USER_ONLY_TARGETS: frozenset[PacketStatus] = frozenset({S.COMPLETED, S.ARCHIVED})


def can_transition(from_status: PacketStatus, to_status: PacketStatus, actor: Actor) -> bool:
    if to_status in ALLOWED_TRANSITIONS[from_status]:
        return True
    return (
        actor.is_user
        and to_status in USER_ONLY_TARGETS
        and bool(ALLOWED_TRANSITIONS[from_status])  # not already terminal
    )


def transition(
    packet: Packet,
    to_status: PacketStatus | str,
    actor: Actor,
    reason: str | None = None,
    *,
    session: Session | None = None,
) -> Event:
    """Validate and apply a status change, writing one ``events`` row. Returns the event."""
    session = session or object_session(packet)
    if session is None:
        raise ValueError("packet is not attached to a session")
    target = PacketStatus(to_status)
    current = PacketStatus(packet.status)

    assert_mutable(packet)  # locked packets: ImmutableRecordError before anything else
    if not is_head(session, packet):
        raise ImmutableRecordError(f"{packet.ref} v{packet.version} is superseded (history)")
    if not isinstance(actor, Actor):
        raise TypeError("transition needs a typed Actor (see orchestrator.actors)")
    if not can_transition(current, target, actor):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        raise InvalidTransitionError(
            f"{packet.ref}: {current.value} → {target.value} is not allowed for {actor} "
            f"(allowed: {allowed or 'none — terminal'})"
        )

    if target is S.COMPLETED and not actor.is_user:
        _completion_gate(session, packet, current)

    before = packet_snapshot(packet)
    packet.status = target
    if target in LOCKING_STATUSES:
        # Same flush → same UPDATE statement; the DB trigger verifies both are set.
        packet.locked = True
        packet.completed_at = utcnow()
    event = record_event(
        session,
        actor=actor,
        action="packet.transition",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before=before,
        after=packet_snapshot(packet),
        reason=reason,
    )
    session.flush()
    return event


def _completion_gate(session: Session, packet: Packet, current: PacketStatus) -> None:
    """System completion locks a packet, so it needs evidence (B9, D2 TC-52, B5 roll-up)."""
    from orchestrator.evaluation.readiness import completion_blockers

    blockers = completion_blockers(session, packet, current)
    if blockers:
        raise InvalidTransitionError(f"{packet.ref} cannot complete yet: " + "; ".join(blockers))
