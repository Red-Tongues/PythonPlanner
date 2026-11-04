"""Human governance of the generation queue (B9): waivers, queue overrides, dependencies.

* **Waiver** — a user accepts one disputed, null or below-gate *soft* field for one exact
  genPacket version. Hard gates (presets with ``hard``) can never be waived (TC-45). A waiver
  lapses when the packet is revised (it names ``target_version``) and can be revoked.
* **Queue override** — BLOCK > HOLD > FORCE > PIN > REORDER (precedence). FORCE, PIN and REORDER
  change dispatch *priority only*: they never bypass readiness (TC-46, TC-47). They are always
  bound to one version. BLOCK and HOLD declare their scope (``version`` or ``lineage``) so a
  protective restriction never disappears by accident.
* **Revocation** — its own append-only record; the original row is never edited. Waivers,
  overrides and dependencies are all revoked this way (dependencies are append-only, M2.1 fix 6).
* **Dependency** — a typed edge (``finish_to_start``); hierarchy is not a dependency.

Every one of these writes bumps the packet's gate revision in the same transaction, so a
concurrent admission or claim that checked the old gate state loses (M2.1 fix 2).

Only user actors make these decisions; every one is evented.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import DependencyKind, EvaPhase, OverrideKind, OverrideScope
from orchestrator.db.models import (
    Packet,
    PacketDependency,
    QueueOverride,
    Revocation,
    Waiver,
    new_id,
)
from orchestrator.errors import NotFoundError, OrchestratorError
from orchestrator.events import record_event
from orchestrator.gates import bump_gate
from orchestrator.repository import get_packet


class GovernanceError(OrchestratorError):
    """A human decision that the rules do not allow (e.g. waiving a hard gate)."""


def _require_user(actor: Actor) -> None:
    if not actor.is_user:
        raise PermissionError("only a user may waive, override or revoke")


def grant_waiver(
    session: Session,
    packet_ref: str,
    field_name: str,
    *,
    actor: Actor,
    reason: str,
    phase: EvaPhase = EvaPhase.PRE,
) -> Waiver:
    from orchestrator.evaluation.readiness import current_presets_by_field

    _require_user(actor)
    packet = get_packet(session, packet_ref)
    preset = current_presets_by_field(session, phase).get(field_name)
    if preset is None:
        raise NotFoundError(f"no {phase.value}-phase preset for field {field_name!r}")
    if preset.hard:
        raise GovernanceError(
            f"{field_name} is a hard gate (schema, dependency, safety or policy) and can never "
            "be waived"
        )
    if not reason.strip():
        raise GovernanceError("a waiver needs a reason")
    waiver = Waiver(
        id=new_id(),
        packet_id=packet.id,
        packet_ref=packet.ref,
        target_version=packet.version,
        field_name=field_name,
        phase=phase,
        actor=str(actor),
        reason=reason,
    )
    session.add(waiver)
    record_event(
        session,
        actor=actor,
        action="waiver.granted",
        entity_type="waiver",
        entity_id=waiver.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"field": field_name, "phase": phase.value, "target_version": packet.version},
        reason=reason,
    )
    session.flush()
    bump_gate(session, packet, actor=actor, cause="waiver granted")
    return waiver


def add_override(
    session: Session,
    packet_ref: str,
    kind: OverrideKind | str,
    *,
    actor: Actor,
    reason: str,
    scope: OverrideScope | str = OverrideScope.VERSION,
    value: float | None = None,
) -> QueueOverride:
    _require_user(actor)
    kind, scope = OverrideKind(kind), OverrideScope(scope)
    if scope is OverrideScope.LINEAGE and kind not in (OverrideKind.BLOCK, OverrideKind.HOLD):
        raise GovernanceError("only BLOCK and HOLD may apply to the whole lineage")
    if kind is OverrideKind.REORDER and value is None:
        raise GovernanceError("REORDER needs a priority delta (value)")
    packet = get_packet(session, packet_ref)
    override = QueueOverride(
        id=new_id(),
        packet_id=packet.id,
        packet_ref=packet.ref,
        kind=kind,
        scope=scope,
        target_version=packet.version,
        value=value,
        actor=str(actor),
        reason=reason,
    )
    session.add(override)
    record_event(
        session,
        actor=actor,
        action="queue_override.added",
        entity_type="queue_override",
        entity_id=override.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"kind": kind.value, "scope": scope.value, "value": value},
        reason=reason,
    )
    session.flush()
    bump_gate(session, packet, actor=actor, cause=f"{kind.value} override added")
    return override


def revoke(
    session: Session, target_type: str, target_id: str, *, actor: Actor, reason: str
) -> Revocation:
    _require_user(actor)
    model = {"waiver": Waiver, "override": QueueOverride, "dependency": PacketDependency}.get(
        target_type
    )
    if model is None:
        raise GovernanceError("target_type must be 'waiver', 'override' or 'dependency'")
    target = session.get(model, target_id)
    if target is None:
        raise NotFoundError(f"no {target_type} {target_id}")
    existing = session.scalar(
        select(Revocation).where(
            Revocation.target_type == target_type, Revocation.target_id == target_id
        )
    )
    if existing is not None:
        return existing
    packet = session.get(Packet, target.packet_id)
    assert packet is not None
    revocation = Revocation(
        id=new_id(), target_type=target_type, target_id=target_id, actor=str(actor), reason=reason
    )
    session.add(revocation)
    record_event(
        session,
        actor=actor,
        action=f"{target_type}.revoked",
        entity_type="revocation",
        entity_id=revocation.id,
        entity_ref=packet.ref,
        after={"target_type": target_type, "target_id": target_id},
        reason=reason,
    )
    session.flush()
    bump_gate(session, packet, actor=actor, cause=f"{target_type} revoked")
    return revocation


def add_dependency(
    session: Session,
    packet_ref: str,
    depends_on_ref: str,
    *,
    actor: Actor,
    kind: DependencyKind | str = DependencyKind.FINISH_TO_START,
    reason: str | None = None,
) -> PacketDependency:
    packet = get_packet(session, packet_ref)
    target = get_packet(session, depends_on_ref)
    if packet.ref == target.ref:
        raise GovernanceError("a packet cannot depend on itself")
    dependency = PacketDependency(
        id=new_id(),
        packet_id=packet.id,
        depends_on_id=target.id,
        kind=DependencyKind(kind),
        satisfied_when="completed",
    )
    session.add(dependency)
    record_event(
        session,
        actor=actor,
        action="dependency.added",
        entity_type="packet_dependency",
        entity_id=dependency.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"depends_on": target.ref, "kind": dependency.kind.value},
        reason=reason,
    )
    session.flush()
    bump_gate(session, packet, actor=actor, cause="dependency added")
    return dependency


def active_overrides(session: Session, packet: Packet) -> list[QueueOverride]:
    """Unrevoked overrides that apply to this packet version (lineage scope or same version)."""
    revoked = select(Revocation.target_id).where(Revocation.target_type == "override")
    rows = session.scalars(
        select(QueueOverride).where(
            QueueOverride.packet_ref == packet.ref, QueueOverride.id.not_in(revoked)
        )
    )
    return [
        o
        for o in rows
        if OverrideScope(o.scope) is OverrideScope.LINEAGE or o.target_version == packet.version
    ]
