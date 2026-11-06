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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

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


def _admission_event(
    session: Session, packet: Packet, actor: Actor, before: str, after: str, reason: str
) -> None:
    record_event(
        session,
        actor=actor,
        action="packet.transition",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"status": before, "gate_revision": packet.gate_revision},
        after={"status": after, "gate_revision": packet.gate_revision},
        reason=reason,
    )


def enforce_failure_budgets(
    session: Session, *, now: datetime | None = None, actor: Actor = KERNEL
) -> list[str]:
    """B4 / M2.1 fix 12: a packet that exhausted its lifetime failure budget moves to
    ``awaiting_confirmation`` (audited) with a confirmation a user approves (another budget:
    failures before the approval stop counting) or rejects (the packet fails)."""
    from orchestrator.contracts import Channel, Stage
    from orchestrator.db.models import Confirmation
    from orchestrator.gates import note_confirmation

    moved: list[str] = []
    for row in queue_rows(session, now):
        if row.ineligible_reason != "failure_budget":
            continue
        packet = session.get(Packet, row.packet_id)
        assert packet is not None
        _transition(
            session,
            packet,
            PacketStatus.AWAITING_CONFIRMATION,
            actor,
            f"lifetime failure budget exhausted ({row.failed_runs} failed runs)",
        )
        stage = (
            Stage.WORKBENCH_PRODUCTION
            if packet.channel == Channel.WORKBENCH_PRODUCTION
            else Stage.LOCAL_GENERATION
        )
        confirmation = Confirmation(
            id=new_id(),
            stage=stage,
            entity_type="failure_budget",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            summary=(
                f"{packet.ref} v{packet.version} failed {row.failed_runs} times: approve to "
                "retry with a fresh budget, reject to mark it failed"
            ),
            payload={"failed_runs": row.failed_runs},
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
            after={"stage": stage.value, "kind": "failure_budget", "failed_runs": row.failed_runs},
            reason="lifetime failure budget exhausted (B4)",
        )
        session.flush()
        note_confirmation(session, confirmation, actor=actor)
        moved.append(packet.ref)
    return moved


def admit_next(
    session: Session, *, now: datetime | None = None, actor: Actor = KERNEL
) -> Packet | None:
    """Admit the best eligible ``ready`` packet that passes readiness (ready → queued).

    Atomic (M2.1 fix 11): the transition is one guarded UPDATE that also compares the gate
    revision and context read before the readiness check (fixes 1 and 2), so two concurrent
    ticks can never both admit a packet, and a governance change in between loses the race.
    """
    from orchestrator.evaluation.readiness import readiness
    from orchestrator.gates import guarded_status_update

    enforce_failure_budgets(session, now=now, actor=actor)
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
        session.refresh(packet)
        observed = packet.gate_revision
        report = readiness(session, packet)
        if not report.ready:
            continue
        if not guarded_status_update(
            session,
            packet,
            expected=PacketStatus.READY,
            observed_gate=observed,
            values={"status": PacketStatus.QUEUED, "updated_at": utcnow()},
        ):
            continue  # lost a race; the next tick sees the new state
        _admission_event(session, packet, actor, "ready", "queued", f"admitted ({row.queue_class})")
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
                "gate_revision": observed,
            },
        )
        session.flush()
        return packet
    return None


def evaluation_snapshot(session: Session, packet: Packet) -> dict[str, Any]:
    """Everything the run is based on, frozen into ``generation_runs`` (D1). Built in the claim
    transaction from the same gate state the claim compares (M2.1 fix 2)."""
    from orchestrator.evaluation.evaluator import head_context
    from orchestrator.governance import active_overrides

    consensus = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT field_name, item_key, status, final_value, confidence_e4, eva_packet_id "
                "FROM v_field_consensus WHERE packet_id = :id AND phase = 'pre' "
                "ORDER BY field_name, item_key"
            ),
            {"id": packet.id},
        ).mappings()
    ]
    revoked = select(Revocation.target_id).where(Revocation.target_type == "waiver")
    waivers = session.scalars(
        select(Waiver.id).where(
            Waiver.packet_id == packet.id,
            Waiver.target_version == packet.version,
            Waiver.phase == EvaPhase.PRE,
            Waiver.id.not_in(revoked),
        )
    ).all()
    project, objective = head_context(session, packet)
    return {
        "packet": {
            "ref": packet.ref,
            "version": packet.version,
            "input_hash": packet.input_hash,
            "gate_revision": packet.gate_revision,
        },
        "context": {
            "project": [project.ref, project.version],
            "objective": [objective.ref, objective.version] if objective else None,
        },
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
    before_cas: Callable[[], None] | None = None,
) -> GenerationRun:
    """Atomically claim a queued packet for generation (queued → running).

    The gate revision is read first; context, overrides and readiness are checked and the
    snapshot is built; then one guarded UPDATE compares the revision, the input hash, the
    context heads, the status and the lease (M2.1 fixes 1 and 2). Any governance write
    committed meanwhile changes the revision, so the claim loses instead of running on stale
    gates. ``before_cas`` is a test seam called just before the UPDATE.
    """
    from orchestrator.evaluation.readiness import readiness
    from orchestrator.gates import guarded_status_update
    from orchestrator.governance import active_overrides
    from orchestrator.repository import get_packet
    from orchestrator.revisions import has_undrained

    now = now or utcnow()
    packet = get_packet(session, packet_ref)
    session.refresh(packet)
    observed = packet.gate_revision
    fence = packet.fencing_token
    if PacketStatus(packet.status) is not PacketStatus.QUEUED:
        raise ClaimError(f"{packet.ref} is {PacketStatus(packet.status).value}, not queued")
    if has_undrained(session, packet.id):  # M3a AC8: history first, then dispatch
        raise ClaimError(f"{packet.ref} has undrained revisions; drain the outbox first")
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
    run_id = new_id()
    if before_cas is not None:
        before_cas()
    won = guarded_status_update(
        session,
        packet,
        expected=PacketStatus.QUEUED,
        observed_gate=observed,
        observed_fence=fence,
        values={
            "status": PacketStatus.RUNNING,
            "claimed_by": worker,
            "lease_expires_at": lease,
            "heartbeat_at": now,
            "updated_at": now,
            "fencing_token": fence + 1,  # C26: every claim carries a new, higher token
            "active_run_id": run_id,  # E7: the run that owns this packet version
        },
    )
    if not won:
        raise ClaimError(
            f"{packet.ref}: claim lost (gates, context or version changed, or claimed by another)"
        )
    channel = packet.channel
    run = GenerationRun(
        id=run_id,
        packet_id=packet.id,
        packet_version=packet.version,
        input_hash=packet.input_hash or "",
        claimed_by=worker,
        state=GenerationRunState.PLANNED,  # C24: the run state machine starts here
        evaluation_snapshot=snapshot,
        channel=channel,
        fencing_token=fence + 1,
        gate_revision=observed,
        lease_expires_at=lease,
        heartbeat_at=now,
        max_attempts=get_int(session, f"attempts.{channel.value}", 1) if channel else None,
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
        after={
            "status": "running",
            "worker": worker,
            "lease_expires_at": lease.isoformat(),
            "gate_revision": observed,
            "fencing_token": fence + 1,
        },
        reason="atomic claim after readiness re-check",
    )
    _admission_event(session, packet, actor, "queued", "running", f"claimed by {worker}")
    session.flush()
    return run
