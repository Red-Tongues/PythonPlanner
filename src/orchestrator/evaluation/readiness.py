"""Readiness: the deterministic rule that admits a genPacket to the generation queue (B9, D1).

``assess_readiness`` is a **pure function** of plain data and is the single authority
(``v_ready_candidates`` only explains). It is evaluated after each evaluation cycle, again at
queue admission and again at the atomic claim, so nothing can slip through between check and
use (D1 point 4, D4 risk 5).

Per field (presets give tier, required, hard and an optional value gate):

* tier 3 or not required → never gates (D2 ruling 8, TC-51);
* missing or pending → blocks, cannot be waived (evaluation still to do — TC-35..38);
* accepted and the gate passes → fine;
* accepted below the gate, disputed or null → a **hard** field blocks and needs review (never
  waivable — TC-45); a **soft** field blocks unless a valid waiver covers it (TC-39..44, TC-47);
* a waiver is valid only for the exact packet version and only until revoked.

Then: every typed dependency satisfied (TC-48) and no pending approval (TC-49). Queue overrides
are *not* readiness: BLOCK/HOLD stop admission, FORCE/PIN only change order and never waive
anything (TC-46, TC-47, TC-50).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from orchestrator.actors import Actor
from orchestrator.contracts import (
    ConfirmationStatus,
    EvaPhase,
    FieldKind,
    PacketStatus,
    Stage,
)
from orchestrator.db.models import (
    Artifact,
    Confirmation,
    EvaPacket,
    EvaPreset,
    Packet,
    PacketDependency,
    Revocation,
    Waiver,
    new_id,
)
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event


class StaleWriteError(OrchestratorError):
    """A projection write found a different input hash: the packet changed meanwhile (B9)."""


@dataclass(frozen=True)
class FieldGate:
    field: str
    tier: int
    required: bool
    hard: bool
    kind: FieldKind
    status: str | None  # accepted | disputed | null | pending | None (missing)
    value: str | None = None  # accepted scalar value (normalised text)
    gate_operator: str | None = None
    gate_value: str | None = None


@dataclass(frozen=True)
class ReadinessInputs:
    fields: list[FieldGate]
    waived: frozenset[str] = frozenset()
    unmet_dependencies: tuple[str, ...] = ()
    pending_approvals: int = 0


@dataclass
class ReadinessReport:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    waived: list[str] = field(default_factory=list)
    needs_review: bool = False  # a human must act (dispute, hard failure, below gate)
    waivable: list[str] = field(default_factory=list)  # soft fields a waiver would clear

    @property
    def ok(self) -> bool:
        return self.ready


def gate_passes(gate: FieldGate) -> bool:
    if gate.gate_operator is None:
        return True
    value, target = gate.value, gate.gate_value
    if value is None or target is None:
        return False
    if gate.kind is FieldKind.SCORE and gate.gate_operator in (">=", ">", "<=", "<", "=", "!="):
        try:
            a, b = Decimal(value), Decimal(target)
        except InvalidOperation:
            return False
        return {
            ">=": a >= b,
            ">": a > b,
            "<=": a <= b,
            "<": a < b,
            "=": a == b,
            "!=": a != b,
        }[gate.gate_operator]
    if gate.gate_operator == "=":
        return value == target
    if gate.gate_operator == "!=":
        return value != target
    return False


def assess_fields(inputs: ReadinessInputs) -> ReadinessReport:
    report = ReadinessReport(ready=True)
    for g in inputs.fields:
        if g.tier == 3 or not g.required:
            continue
        if g.status is None:
            report.blockers.append(f"{g.field}: not evaluated")
            continue
        if g.status == "pending":
            report.blockers.append(f"{g.field}: evaluation pending")
            continue
        if g.status == "accepted" and gate_passes(g):
            continue
        problem = (
            f"{g.field}: {g.value} fails gate {g.gate_operator} {g.gate_value}"
            if g.status == "accepted"
            else f"{g.field}: {g.status}"
        )
        if g.hard:
            report.blockers.append(f"{problem} (hard gate; cannot be waived)")
            report.needs_review = True
        elif g.field in inputs.waived:
            report.waived.append(g.field)
        else:
            report.blockers.append(problem)
            report.waivable.append(g.field)
            report.needs_review = True
    report.ready = not report.blockers
    return report


def assess_readiness(inputs: ReadinessInputs) -> ReadinessReport:
    """The authoritative predicate (pure; D1 point 4)."""
    report = assess_fields(inputs)
    for dependency in inputs.unmet_dependencies:
        report.blockers.append(f"dependency {dependency} not completed")
    if inputs.pending_approvals:
        report.blockers.append(f"{inputs.pending_approvals} approval(s) pending")
    report.ready = not report.blockers
    return report


# --------------------------------------------------------------------------- loading from the DB


def current_presets_by_field(session: Session, phase: EvaPhase) -> dict[str, EvaPreset]:
    latest: dict[str, EvaPreset] = {}
    for preset in session.scalars(
        select(EvaPreset).where(EvaPreset.phase == phase).order_by(EvaPreset.revision)
    ):
        latest[preset.field_name] = preset
    return {k: v for k, v in latest.items() if v.enabled}


def field_gates(session: Session, packet: Packet, phase: EvaPhase) -> list[FieldGate]:
    statuses = {
        r.field_name: r
        for r in session.execute(
            text(
                "SELECT field_name, status, kind FROM v_field_status "
                "WHERE packet_id = :id AND phase = :phase"
            ),
            {"id": packet.id, "phase": phase.value},
        )
    }
    values = dict(
        session.execute(
            text(
                "SELECT field_name, final_value FROM v_field_consensus WHERE packet_id = :id "
                "AND phase = :phase AND item_key = '' AND status = 'accepted'"
            ),
            {"id": packet.id, "phase": phase.value},
        ).all()
    )
    from orchestrator.db.models import CandidateFieldDefinitionRow

    kinds = {
        d.name: FieldKind(d.kind) for d in session.scalars(select(CandidateFieldDefinitionRow))
    }
    gates = []
    for name, preset in current_presets_by_field(session, phase).items():
        row = statuses.get(name)
        gates.append(
            FieldGate(
                field=name,
                tier=preset.tier,
                required=preset.required,
                hard=preset.hard,
                kind=kinds.get(name, FieldKind.SCORE),
                status=row.status if row is not None else None,
                value=values.get(name),
                gate_operator=preset.gate_operator,
                gate_value=preset.gate_value,
            )
        )
    return gates


def valid_waivers(session: Session, packet: Packet, phase: EvaPhase) -> frozenset[str]:
    revoked = select(Revocation.target_id).where(Revocation.target_type == "waiver")
    rows = session.scalars(
        select(Waiver.field_name).where(
            Waiver.packet_id == packet.id,
            Waiver.target_version == packet.version,
            Waiver.phase == phase,
            Waiver.id.not_in(revoked),
        )
    )
    return frozenset(rows)


def unmet_dependencies(session: Session, packet: Packet) -> tuple[str, ...]:
    from orchestrator.repository import get_packet

    lineage = select(Packet.id).where(Packet.ref == packet.ref)
    unmet: list[str] = []
    for dep in session.scalars(
        select(PacketDependency).where(PacketDependency.packet_id.in_(lineage))
    ):
        target = session.get(Packet, dep.depends_on_id)
        assert target is not None
        head = get_packet(session, target.ref)
        if PacketStatus(head.status) is not PacketStatus.COMPLETED and target.ref not in unmet:
            unmet.append(target.ref)
    return tuple(unmet)


def pending_approvals(session: Session, packet: Packet) -> int:
    return len(
        session.scalars(
            select(Confirmation.id).where(
                Confirmation.entity_id == packet.id,
                Confirmation.status == ConfirmationStatus.PENDING,
            )
        ).all()
    )


def evaluation_verdict(session: Session, packet: Packet, phase: EvaPhase) -> ReadinessReport:
    """Fields only (with waivers): what an evaluation cycle concludes."""
    return assess_fields(
        ReadinessInputs(field_gates(session, packet, phase), valid_waivers(session, packet, phase))
    )


def readiness(session: Session, packet: Packet) -> ReadinessReport:
    """Full readiness of a genPacket version (fields, waivers, dependencies, approvals)."""
    return assess_readiness(
        ReadinessInputs(
            fields=field_gates(session, packet, EvaPhase.PRE),
            waived=valid_waivers(session, packet, EvaPhase.PRE),
            unmet_dependencies=unmet_dependencies(session, packet),
            pending_approvals=pending_approvals(session, packet),
        )
    )


def completion_blockers(session: Session, packet: Packet, current: PacketStatus) -> list[str]:
    """Why a system actor may not complete (lock) this packet yet."""
    if current is PacketStatus.WAITING_FOR_CHILDREN:
        from orchestrator.branching import children_of

        children = children_of(session, packet)
        if not children:
            return ["the trunk has no children"]
        return [
            f"child {c.ref} is {PacketStatus(c.status).value}"
            for c in children
            if PacketStatus(c.status) is not PacketStatus.COMPLETED
        ]
    if current is not PacketStatus.EVALUATING_POST:
        return [f"system completion only follows evaluating_post (now {current.value})"]
    report = evaluation_verdict(session, packet, EvaPhase.POST)
    blockers = list(report.blockers)
    artifact = session.scalar(
        select(Artifact)
        .where(Artifact.packet_id == packet.id)
        .order_by(Artifact.created_at.desc())
        .limit(1)
    )
    if artifact is not None:
        stale = session.scalars(
            select(EvaPacket.field_name).where(
                EvaPacket.packet_id == packet.id,
                EvaPacket.phase == EvaPhase.POST,
                EvaPacket.target_hash != artifact.sha256,
                EvaPacket.state != "superseded",
            )
        ).all()
        current_ok = session.scalars(
            select(EvaPacket.field_name).where(
                EvaPacket.packet_id == packet.id,
                EvaPacket.phase == EvaPhase.POST,
                EvaPacket.target_hash == artifact.sha256,
            )
        ).all()
        missing = sorted(set(stale) - set(current_ok))
        if missing:
            blockers.append(f"post-evaluation of {missing} does not target the final artifact")
    return blockers


# --------------------------------------------------------------------------- writes


def write_projections(session: Session, packet: Packet, target: str, actor: Actor) -> None:
    """Write ``packets.tags`` / ``scores`` from ``v_field_consensus`` with the stale-write guard
    ``WHERE id = :id AND input_hash = :target`` (B9). Evented."""
    tags: dict[str, list[str]] = {}
    scores: dict[str, Any] = {}
    for row in session.execute(
        text(
            "SELECT field_name, kind, item_key, final_value FROM v_field_consensus "
            "WHERE packet_id = :id AND phase = 'pre' AND status = 'accepted' "
            "ORDER BY field_name, item_key"
        ),
        {"id": packet.id},
    ):
        if row.kind == "tagset":
            tags.setdefault(row.field_name, []).append(row.item_key)
        elif row.kind == "score":
            scores[row.field_name] = int(row.final_value)
        elif row.kind == "bool":
            scores[row.field_name] = row.final_value == "true"
        else:
            scores[row.field_name] = row.final_value
    before = {"tags": packet.tags, "scores": packet.scores}
    record_event(
        session,
        actor=actor,
        action="packet.projections",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before=before,
        after={"tags": tags, "scores": scores, "input_hash": target},
        reason="consensus projections refreshed from v_field_consensus",
    )
    session.flush()
    result = session.connection().execute(
        update(Packet.__table__)
        .where(Packet.__table__.c.id == packet.id, Packet.__table__.c.input_hash == target)
        .values(tags=tags, scores=scores)
    )
    if result.rowcount != 1:
        raise StaleWriteError(f"{packet.ref}: inputs changed; projections not written")
    set_committed_value(packet, "tags", tags)
    set_committed_value(packet, "scores", scores)


def request_review(
    session: Session, packet: Packet, report: ReadinessReport, actor: Actor
) -> Confirmation | None:
    """Put a packet that needs a human on the approval queue (disputed key fields, M2 §5)."""
    existing = session.scalar(
        select(Confirmation).where(
            Confirmation.entity_id == packet.id,
            Confirmation.entity_type == "packet_review",
            Confirmation.status == ConfirmationStatus.PENDING,
        )
    )
    if existing is not None:
        return existing
    confirmation = Confirmation(
        id=new_id(),
        stage=Stage.CANDIDATE_PROMOTION,
        entity_type="packet_review",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        summary=f"Review {packet.ref} v{packet.version}: " + "; ".join(report.blockers),
        payload={"blockers": report.blockers, "waivable": report.waivable},
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
        after={
            "stage": confirmation.stage.value,
            "kind": "packet_review",
            "waivable": report.waivable,
        },
        reason="evaluation needs a human decision",
    )
    session.flush()
    return confirmation
