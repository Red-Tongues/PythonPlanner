"""The generation queue (PROJECT_PLAN §8, B4, B9): rank, admission and the atomic claim.

* **Rank** is SQL: ``v_queue`` (live) or ``queue_rows(now=...)`` — the same query with a bound
  ``now`` so tests can simulate time on both dialects. Eligibility (``eligible``,
  ``ineligible_reason``) is separate from rank (``priority``, ``queue_class``, ``position``).
* **Admission** (``ready → queued``) re-checks the authoritative readiness predicate. Fairness
  (B9): after N consecutive FORCE/PIN admissions, the next admission goes to the best normal
  packet if one is eligible (N = ``queue.fair_admission_every``).
* **Claim** (``queued → running``) re-checks readiness again and then wins the packet with a
  guarded UPDATE (``WHERE id AND version AND input_hash AND status = 'queued' AND`` unclaimed or
  lease expired). Only one worker can win; an edit just before the claim makes it fail. The
  claim writes a ``generation_runs`` row with the frozen evaluation snapshot (D1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from orchestrator.actors import Actor
from orchestrator.contracts import EvaPhase, GenerationRunState, OverrideKind, PacketStatus
from orchestrator.db.models import (
    GenerationRun,
    Packet,
    QueueAdmission,
    Revocation,
    Waiver,
    new_id,
    utcnow,
)
from orchestrator.db.types import UTCDateTime
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event
from orchestrator.settings_store import get_int
from orchestrator.sql.views import queue_select

KERNEL = Actor.system("kernel")
QUEUE_CLASSES = {1: "force", 2: "pin", 3: "max_wait", 4: "normal"}


class ClaimError(OrchestratorError):
    """The claim lost: the packet changed, is not ready, or another worker has it."""


@dataclass(frozen=True)
class QueueRow:
    packet_id: str
    packet_ref: str
    packet_version: int
    status: str
    eligible: bool
    ineligible_reason: str | None
    queue_class: str
    priority: float
    age_hours: float
    failed_runs: int
    position: int | None


def queue_rows(session: Session, now: datetime | None = None) -> list[QueueRow]:
    """``v_queue`` evaluated at ``now`` (defaults to the database clock)."""
    dialect = session.get_bind().dialect.name
    if now is None:
        sql = text("SELECT * FROM v_queue")
        params: dict[str, Any] = {}
    else:
        now_expr = "CAST(:now AS TIMESTAMP WITH TIME ZONE)" if dialect == "postgresql" else ":now"
        sql = text(queue_select(dialect, now_expr)).bindparams(
            bindparam("now", type_=UTCDateTime())
        )
        params = {"now": now}
    rows = session.execute(sql, params).mappings().all()
    result = [
        QueueRow(
            packet_id=r["packet_id"],
            packet_ref=r["packet_ref"],
            packet_version=r["packet_version"],
            status=r["status"],
            eligible=bool(r["eligible"]),
            ineligible_reason=r["ineligible_reason"],
            queue_class=QUEUE_CLASSES[int(r["queue_class"])],
            priority=float(r["priority"]),
            age_hours=float(r["age_hours"]),
            failed_runs=int(r["failed_runs"]),
            position=int(r["position"]) if r["position"] is not None else None,
        )
        for r in rows
    ]
    return sorted(result, key=lambda r: (r.position is None, r.position or 0, r.packet_ref))


def _transition(session: Session, packet: Packet, status: PacketStatus, actor: Actor, why: str):
    from orchestrator.lifecycle import transition

    return transition(packet, status, actor, why, session=session)


def admit_next(
    session: Session, *, now: datetime | None = None, actor: Actor = KERNEL
) -> Packet | None:
    """Admit the best eligible ``ready`` packet that passes readiness (ready → queued)."""
    from orchestrator.evaluation.readiness import readiness

    rows = [r for r in queue_rows(session, now) if r.eligible and r.status == "ready"]
    if not rows:
        return None
    every = get_int(session, "queue.fair_admission_every", 3)
    recent = session.scalars(
        select(QueueAdmission.admission_class)
        .order_by(QueueAdmission.created_at.desc(), QueueAdmission.id.desc())
        .limit(every)
    ).all()
    starving = len(recent) == every and all(c in ("force", "pin") for c in recent)
    if starving and any(r.queue_class not in ("force", "pin") for r in rows):
        rows = [r for r in rows if r.queue_class not in ("force", "pin")]
    for row in rows:
        packet = session.get(Packet, row.packet_id)
        assert packet is not None
        report = readiness(session, packet)
        if not report.ready:
            continue
        _transition(session, packet, PacketStatus.QUEUED, actor, f"admitted ({row.queue_class})")
        admission = QueueAdmission(
            id=new_id(),
            packet_id=packet.id,
            packet_version=packet.version,
            admission_class=row.queue_class,
        )
        session.add(admission)
        record_event(
            session,
            actor=actor,
            action="queue.admitted",
            entity_type="queue_admission",
            entity_id=admission.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            after={
                "class": row.queue_class,
                "priority": row.priority,
                "fair_reserved": starving,
                "waived": report.waived,
            },
        )
        session.flush()
        return packet
    return None


def evaluation_snapshot(session: Session, packet: Packet) -> dict[str, Any]:
    """Everything the run is based on, frozen into ``generation_runs`` (D1)."""
    from orchestrator.governance import active_overrides

    consensus = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT field_name, item_key, status, final_value, confidence, eva_packet_id "
                "FROM v_field_consensus WHERE packet_id = :id AND phase = 'pre' "
                "ORDER BY field_name, item_key"
            ),
            {"id": packet.id},
        ).mappings()
    ]
    for row in consensus:
        if row["confidence"] is not None:
            row["confidence"] = str(row["confidence"])
    revoked = select(Revocation.target_id).where(Revocation.target_type == "waiver")
    waivers = session.scalars(
        select(Waiver.id).where(
            Waiver.packet_id == packet.id,
            Waiver.target_version == packet.version,
            Waiver.phase == EvaPhase.PRE,
            Waiver.id.not_in(revoked),
        )
    ).all()
    return {
        "packet": {"ref": packet.ref, "version": packet.version, "input_hash": packet.input_hash},
        "tags": packet.tags,
        "scores": packet.scores,
        "consensus": consensus,
        "waivers": list(waivers),
        "overrides": [o.id for o in active_overrides(session, packet)],
    }


def claim(
    session: Session,
    packet_ref: str,
    worker: str,
    *,
    now: datetime | None = None,
    actor: Actor = KERNEL,
) -> GenerationRun:
    """Atomically claim a queued packet for generation (queued → running)."""
    from orchestrator.evaluation.readiness import readiness
    from orchestrator.governance import active_overrides
    from orchestrator.repository import get_packet

    now = now or utcnow()
    packet = get_packet(session, packet_ref)
    if PacketStatus(packet.status) is not PacketStatus.QUEUED:
        raise ClaimError(f"{packet.ref} is {PacketStatus(packet.status).value}, not queued")
    blocking = [
        o
        for o in active_overrides(session, packet)
        if OverrideKind(o.kind) in (OverrideKind.BLOCK, OverrideKind.HOLD)
    ]
    if blocking:
        raise ClaimError(f"{packet.ref} is {OverrideKind(blocking[0].kind).value}ed")
    report = readiness(session, packet)  # re-checked at the claim (D1 point 4)
    if not report.ready:
        raise ClaimError(f"{packet.ref} is no longer ready: " + "; ".join(report.blockers))
    lease = now + timedelta(minutes=get_int(session, "queue.lease_minutes", 30))
    snapshot = evaluation_snapshot(session, packet)
    session.flush()
    table = Packet.__table__
    result = session.connection().execute(
        update(table)
        .where(
            table.c.id == packet.id,
            table.c.version == packet.version,
            table.c.input_hash == packet.input_hash,
            table.c.status == PacketStatus.QUEUED.value,
            (table.c.claimed_by.is_(None)) | (table.c.lease_expires_at < now),
            ~select(Packet.id).where(Packet.supersedes_id == packet.id).exists(),
        )
        .values(
            status=PacketStatus.RUNNING.value,
            claimed_by=worker,
            lease_expires_at=lease,
            heartbeat_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise ClaimError(f"{packet.ref}: claim lost (edited, re-queued or claimed by another)")
    for attr, value in (
        ("status", PacketStatus.RUNNING),
        ("claimed_by", worker),
        ("lease_expires_at", lease),
        ("heartbeat_at", now),
    ):
        set_committed_value(packet, attr, value)
    run = GenerationRun(
        id=new_id(),
        packet_id=packet.id,
        packet_version=packet.version,
        input_hash=packet.input_hash or "",
        claimed_by=worker,
        state=GenerationRunState.CLAIMED,
        evaluation_snapshot=snapshot,
    )
    session.add(run)
    record_event(
        session,
        actor=actor,
        action="packet.claimed",
        entity_type="generation_run",
        entity_id=run.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"status": "queued"},
        after={"status": "running", "worker": worker, "lease_expires_at": lease.isoformat()},
        reason="atomic claim after readiness re-check",
    )
    record_event(
        session,
        actor=actor,
        action="packet.transition",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"status": "queued"},
        after={"status": "running", "generation_run": run.id},
        reason=f"claimed by {worker}",
    )
    session.flush()
    return run
