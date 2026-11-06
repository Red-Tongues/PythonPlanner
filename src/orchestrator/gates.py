"""Gate state for the atomic admission and claim (review R2, M2.1 fixes 1, 2 and 11).

**Gate revision (fix 2).** Every governance write that can change a genPacket's readiness bumps
``packets.gate_revision`` on the head of its lineage in the *same transaction*: waivers,
overrides, revocations, dependencies, and confirmations about the packet (created or decided).
Admission and claim read the revision before their readiness check and compare-and-set it in
their guarded UPDATE. A concurrent writer that commits in between changes the revision, so the
admission or claim matches no row and loses: no packet runs on a gate state that no longer holds.

**Context (M3a, T1-C v3 §2).** ``packets.input_hash`` is intra-packet only (title, prompt,
response contract, repo path). Upstream project/objective edits no longer send a packet back to
``evaluating_pre``: they raise ``question_context`` through the evaPackets' context links
(``evaluation.context``). M2.1 fix 1's ``require_current_context`` is retired.
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
    if confirmation.entity_type == "project" and confirmation.entity_ref:
        bump_scope(  # e.g. the ingestion confirmation: the whole project's gate (C25)
            session,
            "project",
            confirmation.entity_ref,
            actor=actor,
            cause=f"confirmation {confirmation.status}",
        )
        return
    if confirmation.entity_type not in PACKET_CONFIRMATION_TYPES:
        return
    packet = session.get(Packet, confirmation.entity_id)
    if packet is not None:
        bump_gate(session, packet, actor=actor, cause=f"confirmation {confirmation.status}")


def current_fingerprint(session: Session, packet: Packet) -> str:
    """The packet's current intra-packet hash (what its pre-phase evaPackets target)."""
    from orchestrator.evaluation.context import intra_packet_hash

    return intra_packet_hash(packet)


def guarded_status_update(
    session: Session,
    packet: Packet,
    *,
    expected: PacketStatus,
    observed_gate: int,
    values: dict[str, Any],
    observed_fence: int | None = None,
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
    if observed_fence is not None:  # M3 C26: the claim compares and increments the fence
        conditions.append(table.c.fencing_token == observed_fence)
    if "claimed_by" in values:
        now = values["heartbeat_at"]
        conditions.append(table.c.claimed_by.is_(None) | (table.c.lease_expires_at < now))
    result = session.connection().execute(update(table).where(*conditions).values(**values))
    if result.rowcount != 1:
        return False
    for attr, value in values.items():
        set_committed_value(packet, attr, value)
    return True


# --------------------------------------------------------------------------- C25 scope bumps


def scope_packet_ids(session: Session, scope_type: str, scope_id: str) -> list[str]:
    """Ids of the non-terminal *head* packets in a governance scope (C25).

    ``scope_id`` is a stable ref: ``*`` (global), a project ref, an objective ref, a branch's
    trunk ref (the trunk and every packet whose root is any version of it) or a packet ref.
    """
    from orchestrator.contracts import ScopeType
    from orchestrator.transitions import TERMINAL

    table = Packet.__table__
    successor = table.alias("successor")  # an alias: the subquery must not self-correlate
    heads = ~select(successor.c.id).where(successor.c.supersedes_id == table.c.id).exists()
    open_ = table.c.status.not_in([s.value for s in TERMINAL])
    stmt = select(table.c.id).where(heads, open_, table.c.locked.is_(False))
    kind = ScopeType(scope_type)
    if kind is ScopeType.GLOBAL:
        pass
    elif kind is ScopeType.PROJECT:
        stmt = stmt.where(table.c.project_id.in_(select(Project.id).where(Project.ref == scope_id)))
    elif kind is ScopeType.OBJECTIVE:
        stmt = stmt.where(
            table.c.objective_id.in_(select(Objective.id).where(Objective.ref == scope_id))
        )
    elif kind is ScopeType.BRANCH:
        trunk_ids = select(Packet.id).where(Packet.ref == scope_id)
        stmt = stmt.where((table.c.ref == scope_id) | table.c.root_id.in_(trunk_ids))
    elif kind is ScopeType.PACKET:
        stmt = stmt.where(table.c.ref == scope_id)
    else:  # a channel is not a packet scope: its writes (pauses) do not change readiness
        raise ValueError(f"{kind.value} is not a packet scope")
    return list(session.execute(stmt.order_by(table.c.id)).scalars())


def bump_scope(
    session: Session, scope_type: str, scope_id: str, *, actor: Actor, cause: str
) -> list[str]:
    """C25: one governance write increments the gate revision of *every* non-terminal head
    packet in its scope, in the caller's transaction (one UPDATE, one batch event naming every
    packet). Returns the bumped packet ids."""
    session.flush()
    ids = scope_packet_ids(session, scope_type, scope_id)
    if not ids:
        return []
    table = Packet.__table__
    session.connection().execute(
        update(table).where(table.c.id.in_(ids)).values(gate_revision=table.c.gate_revision + 1)
    )
    for packet in session.identity_map.values():  # keep loaded packets in step with the DB
        if isinstance(packet, Packet) and packet.id in ids:
            set_committed_value(packet, "gate_revision", packet.gate_revision + 1)
    refs = list(session.execute(select(table.c.ref).where(table.c.id.in_(ids))).scalars())
    record_event(
        session,
        actor=actor,
        action="packet.gate_revision",
        entity_type="packet",
        entity_id=None,
        after={"scope": [scope_type, scope_id], "packets": sorted(refs), "cause": cause},
        reason="governance write in scope (C25)",
    )
    session.flush()
    return ids
