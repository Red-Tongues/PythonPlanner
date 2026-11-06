"""The approval queue (Requirement E): list, approve and reject confirmations.

Approving carries out what was proposed, as the approving user:

* ``branching`` — split the packet now (idempotent per rule version);
* ``routing`` — set the proposed channel;
* ``packet_review`` (``candidate_promotion``) — waive the listed *soft* fields for this packet
  version and, if the evaluation set is then acceptable, move it from ``needs_review`` to
  ``ready``. Hard gates are never waived (they stay listed as blockers);
* anything else (e.g. ``ingestion``) — recorded as approved.

Rejecting needs a reason and records the decision. A rejected split holds the packet (D8 row 61)
and is not proposed again for the same packet version and rule version.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from orchestrator.actors import Actor
from orchestrator.contracts import Channel, ConfirmationStatus, EvaPhase, PacketStatus, Stage
from orchestrator.db.models import Confirmation, Packet, Threshold, utcnow
from orchestrator.errors import NotFoundError, OrchestratorError
from orchestrator.events import record_event
from orchestrator.gates import PACKET_CONFIRMATION_TYPES, note_confirmation
from orchestrator.llm import LLMProvider

#: the kernel's readiness decision, made inside an owner's approval transaction
READINESS = Actor.system("readiness")


class ConfirmationError(OrchestratorError):
    """The confirmation cannot be decided (unknown, already decided, or stale)."""


def list_confirmations(session: Session, *, pending_only: bool = True) -> list[Confirmation]:
    stmt = select(Confirmation).order_by(Confirmation.created_at, Confirmation.id)
    if pending_only:
        stmt = stmt.where(Confirmation.status == ConfirmationStatus.PENDING)
    return list(session.scalars(stmt))


def _find(session: Session, confirmation_id: str) -> Confirmation:
    matches = session.scalars(
        select(Confirmation).where(Confirmation.id.startswith(confirmation_id))
    ).all()
    if len(matches) != 1:
        raise NotFoundError(f"no unique confirmation matching {confirmation_id!r}")
    confirmation = matches[0]
    if ConfirmationStatus(confirmation.status) is not ConfirmationStatus.PENDING:
        raise ConfirmationError(f"confirmation {confirmation.id} is already {confirmation.status}")
    return confirmation


def decide_once(
    session: Session,
    confirmation: Confirmation,
    status: ConfirmationStatus,
    actor: Actor,
    reason: str | None,
) -> None:
    """Decide a pending confirmation with one compare-and-set UPDATE (``WHERE status =
    'pending'``): of two concurrent decisions exactly one wins; the loser raises and its
    transaction rolls back (M3 C38: approval racing rejection). The caller writes the event."""

    session.flush()
    table = Confirmation.__table__
    values = {
        "status": status.value,
        "decided_by": str(actor),
        "decided_at": utcnow(),
        "reason": reason,
        "updated_at": utcnow(),
    }
    result = session.connection().execute(
        update(table)
        .where(table.c.id == confirmation.id, table.c.status == ConfirmationStatus.PENDING.value)
        .values(**values)
    )
    if result.rowcount != 1:
        raise ConfirmationError(f"confirmation {confirmation.id} was already decided")
    for attr, value in values.items():
        set_committed_value(
            confirmation, attr, ConfirmationStatus(value) if attr == "status" else value
        )


def _decide(
    session: Session,
    confirmation: Confirmation,
    status: ConfirmationStatus,
    actor: Actor,
    reason: str | None,
    outcome: dict | None = None,
    *,
    update_gate: bool = True,
) -> None:
    decide_once(session, confirmation, status, actor, reason)
    record_event(
        session,
        actor=actor,
        action=f"confirmation.{status.value}",
        entity_type="confirmation",
        entity_id=confirmation.id,
        entity_ref=confirmation.entity_ref,
        entity_version=confirmation.entity_version,
        before={"status": "pending"},
        after={"status": status.value, "stage": Stage(confirmation.stage).value, **(outcome or {})},
        reason=reason,
    )
    session.flush()
    if update_gate:
        note_confirmation(session, confirmation, actor=actor)


def approve(
    session: Session,
    confirmation_id: str,
    *,
    actor: Actor,
    provider: LLMProvider | None = None,
    reason: str | None = None,
) -> dict:
    if not actor.is_user:
        raise PermissionError("only a user may approve a confirmation")
    confirmation = _find(session, confirmation_id)
    stage = Stage(confirmation.stage)
    packet = (
        session.get(Packet, confirmation.entity_id)
        if confirmation.entity_type in PACKET_CONFIRMATION_TYPES
        else None
    )
    outcome: dict = {}
    if packet is not None:
        from orchestrator.repository import is_head

        if not is_head(session, packet):
            raise ConfirmationError(f"{packet.ref} v{packet.version} has been superseded")
    # decide first: a pending approval blocks readiness, and effects below re-check it
    _decide(session, confirmation, ConfirmationStatus.APPROVED, actor, reason)
    if stage is Stage.BRANCHING and packet is not None:
        from orchestrator.branching import split_packet

        if provider is None:
            raise ConfirmationError("approving a split needs the local generation provider")
        rule = session.get(Threshold, confirmation.payload["threshold_id"])
        assert rule is not None
        outcome["children"] = [
            c.ref
            for c in split_packet(
                session, packet, rule, provider, actor=actor, approval=confirmation
            )
        ]
    elif stage is Stage.ROUTING and packet is not None:
        from orchestrator.branching import apply_channel

        channel = Channel(confirmation.payload["channel"])
        apply_channel(session, packet, channel, actor=actor, reason="routing approved")
        outcome["channel"] = channel.value
    elif confirmation.entity_type == "packet_review" and packet is not None:
        outcome.update(_approve_review(session, confirmation, packet, actor, reason))
    record_event(
        session,
        actor=actor,
        action="confirmation.applied",
        entity_type="confirmation",
        entity_id=confirmation.id,
        entity_ref=confirmation.entity_ref,
        after=outcome,
    )
    session.flush()
    return outcome


def _approve_review(
    session: Session,
    confirmation: Confirmation,
    packet: Packet,
    actor: Actor,
    reason: str | None,
) -> dict:
    from orchestrator.evaluation.readiness import evaluation_verdict
    from orchestrator.governance import grant_waiver
    from orchestrator.lifecycle import transition

    waived = []
    for field in confirmation.payload.get("waivable", []):
        grant_waiver(
            session, packet.ref, field, actor=actor, reason=reason or "accepted by the reviewer"
        )
        waived.append(field)
    verdict = evaluation_verdict(session, packet, EvaPhase.PRE)
    if verdict.ok and PacketStatus(packet.status) is PacketStatus.NEEDS_REVIEW:
        # D8: the owner sends a reviewed packet back to evaluating_pre; readiness (a kernel
        # decision, never the approver's) then admits it in the same transaction
        transition(packet, PacketStatus.EVALUATING_PRE, actor, "review approved", session=session)
        transition(
            packet,
            PacketStatus.READY,
            READINESS,
            "evaluation set acceptable after the review",
            session=session,
        )
    return {"waived": waived, "ready": verdict.ok, "blockers": verdict.blockers}


def reject(session: Session, confirmation_id: str, *, actor: Actor, reason: str) -> None:
    if not actor.is_user:
        raise PermissionError("only a user may reject a confirmation")
    if not reason.strip():
        raise ConfirmationError("a rejection needs a reason (D8: rejection of any type)")
    confirmation = _find(session, confirmation_id)
    _decide(session, confirmation, ConfirmationStatus.REJECTED, actor, reason)
    if confirmation.stage is Stage.BRANCHING and confirmation.entity_type == "packet":
        from orchestrator.lifecycle import transition

        packet = session.get(Packet, confirmation.entity_id)
        if packet is not None and PacketStatus(packet.status) is PacketStatus.AWAITING_CONFIRMATION:
            # D8 row 61 (R4 audit J1): a rejection of any type holds the packet with the
            # reason. Readiness stays a kernel decision: the owner releases the hold
            # (held → evaluating_pre) and the kernel re-admits it.
            transition(
                packet,
                PacketStatus.HELD,
                actor,
                f"branch split rejected: {reason}",
                session=session,
            )
