"""Packet lifecycle: an explicit allowed-transition table, applied with an event per change.

Kernel/agent actors may only follow ``ALLOWED_TRANSITIONS``. User actors (``user`` or
``user:<channel>``) may additionally complete or archive any non-terminal packet by hand — the
``manual`` autonomy mode means "the user does it", so a person must be able to close work the
kernel never touched. Completing or archiving locks the packet in the same UPDATE statement.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, object_session

from orchestrator.contracts import LOCKING_STATUSES, PacketStatus
from orchestrator.db.guards import assert_mutable
from orchestrator.db.models import Event, Packet, utcnow
from orchestrator.errors import ImmutableRecordError, InvalidTransitionError
from orchestrator.events import record_event
from orchestrator.repository import is_head, packet_snapshot

S = PacketStatus

ALLOWED_TRANSITIONS: dict[PacketStatus, frozenset[PacketStatus]] = {
    S.DRAFT: frozenset({S.QUEUED, S.ARCHIVED}),
    S.QUEUED: frozenset({S.EVALUATING, S.READY, S.DRAFT, S.ARCHIVED}),
    S.EVALUATING: frozenset({S.READY, S.QUEUED, S.FAILED}),
    S.READY: frozenset({S.IN_PROGRESS, S.AWAITING_CONFIRMATION, S.QUEUED, S.ARCHIVED}),
    S.IN_PROGRESS: frozenset({S.AWAITING_CONFIRMATION, S.PROCESSED, S.READY, S.FAILED}),
    S.AWAITING_CONFIRMATION: frozenset({S.IN_PROGRESS, S.READY, S.PROCESSED, S.FAILED}),
    S.PROCESSED: frozenset({S.COMPLETED, S.IN_PROGRESS, S.QUEUED}),
    S.FAILED: frozenset({S.QUEUED, S.ARCHIVED}),
    S.COMPLETED: frozenset(),
    S.ARCHIVED: frozenset(),
}

#: Extra transitions only a user actor may make, from any non-terminal status.
USER_ONLY_TARGETS: frozenset[PacketStatus] = frozenset({S.COMPLETED, S.ARCHIVED})


def is_user_actor(actor: str) -> bool:
    return actor == "user" or actor.startswith("user:")


def can_transition(from_status: PacketStatus, to_status: PacketStatus, actor: str) -> bool:
    if to_status in ALLOWED_TRANSITIONS[from_status]:
        return True
    return (
        is_user_actor(actor)
        and to_status in USER_ONLY_TARGETS
        and bool(ALLOWED_TRANSITIONS[from_status])  # not already terminal
    )


def transition(
    packet: Packet,
    to_status: PacketStatus | str,
    actor: str,
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
    if not can_transition(current, target, actor):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        raise InvalidTransitionError(
            f"{packet.ref}: {current.value} → {target.value} is not allowed for {actor!r} "
            f"(allowed: {allowed or 'none — terminal'})"
        )

    before = packet_snapshot(packet)
    packet.status = target
    if target in LOCKING_STATUSES:
        # Same flush → same UPDATE statement; the DB trigger verifies both are set.
        packet.locked = True
        packet.completed_at = utcnow()
    session.flush()
    return record_event(
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
