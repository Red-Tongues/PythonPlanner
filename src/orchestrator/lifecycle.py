"""Packet lifecycle: apply one transition of the M3 table (``orchestrator.transitions``) with
an event per change.

Each (from, to) pair names the actor kinds that may make it (C23, D8): kernel moves are
``system`` actors, owner decisions are ``user`` actors, and ``llm`` actors never move a packet.
Completing always passes the completion gate (post-evaluation of the exact artifact, or child
roll-up), for users too (M2.1 fix 5); closing without that evidence is ``closed_unverified``, a
terminal non-success disposition that needs a reason. Every terminal transition locks the
packet in the same UPDATE statement, and migration 0007's trigger rejects any pair not in the
table at the database too.
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
from orchestrator.transitions import PACKET_TRANSITIONS, can_move_packet, check_packet

S = PacketStatus

#: Derived view of ``orchestrator.transitions.PACKET_TRANSITIONS`` (any actor kind): status →
#: the statuses it may move to. The tables themselves live in ``orchestrator.transitions``
#: (M3 C23); this module applies them with an event per change.
ALLOWED_TRANSITIONS: dict[PacketStatus, frozenset[PacketStatus]] = {
    status: frozenset(PacketStatus(t) for (s, t) in PACKET_TRANSITIONS if s == status.value)
    for status in PacketStatus
}


def can_transition(from_status: PacketStatus, to_status: PacketStatus, actor: Actor) -> bool:
    return can_move_packet(from_status, to_status, actor)


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
    try:
        check_packet(current, target, actor)
    except InvalidTransitionError as exc:
        raise InvalidTransitionError(f"{packet.ref}: {exc}") from None
    if target is S.CLOSED_UNVERIFIED and not (reason or "").strip():
        raise InvalidTransitionError(f"{packet.ref}: closing unverified needs a reason (C23)")

    if target is S.COMPLETED:  # every actor, users included (M2.1 fix 5)
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
    """Completion locks a packet as a success, so it needs evidence (B9, D2 TC-52, B5 roll-up,
    M2.1 fixes 3 and 5) — whoever asks."""
    from orchestrator.evaluation.readiness import completion_blockers

    blockers = completion_blockers(session, packet, current)
    if blockers:
        raise InvalidTransitionError(f"{packet.ref} cannot complete yet: " + "; ".join(blockers))
