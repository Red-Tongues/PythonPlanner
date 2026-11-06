"""The generation queue (M3a; R6 OQ1-OQ12, OQ25, OQ31): genQueue, evaQueue, admission, claim.

* **Rank** is SQL (``orchestrator.sql.genqueue``): ``v_gen_queue`` / ``v_eva_queue`` (live) or
  ``gen_queue_rows(now=...)`` / ``eva_queue_rows(now=...)`` — the same queries with a bound
  ``now`` so tests can simulate time on both dialects. All scores are integers (``*_e4``).
* **Epoch snapshots** (OQ31): every admission tick, dispatch and completion writes the genQueue
  as it stands to the append-only ``queue_epoch_snapshot``, with each developing packet's
  lockout flag (OQ3). The lockout and the dispatch choice read the newest snapshot, never a live
  ``ROW_NUMBER``.
* **Admission tick** (``admission_tick``; OQ7, OQ12): drain the outbox, demote a queued packet
  that fell below the floor (``queued → ready``), promote ready packets at or above it (``ready →
  queued``) after the readiness predicate, then snapshot. The floor is 3,500, 2,500 while fewer
  than 3 packets are queued, and is bypassed after 7,200 s in ``ready``.
* **Dispatch choice** (``next_dispatch``; OQ25): the newest snapshot's first queued packet, except
  every 5th dispatch takes the oldest queued one, and a challenger must beat the previous epoch's
  leader by the incumbency margin.
* **Claim** (``queued → running``) re-checks readiness, refuses a packet with undrained revisions
  (AC8) and wins the packet with one guarded UPDATE; it records the claim-time
  ``content_revision`` (OQ20) and writes a dispatch snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import EvaPhase, GenerationRunState, OverrideKind, PacketStatus
from orchestrator.db.models import (
    FeedbackEvent,
    GenerationRun,
    Packet,
    PacketDependency,
    QueueAdmission,
    QueueEpochSnapshot,
    Revocation,
    Waiver,
    new_id,
    utcnow,
)
from orchestrator.db.refs import allocate
from orchestrator.db.types import UTCDateTime
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event
from orchestrator.settings_store import get_int
from orchestrator.sql import genqueue

KERNEL = Actor.system("kernel")
FEEDBACK_KINDS = ("boost", "nudge", "demote", "pin")


class ClaimError(OrchestratorError):
    """The claim lost: the packet changed, is not ready, or another worker has it."""


@dataclass(frozen=True)
class GenQueueRow:
    packet_id: str
    packet_ref: str
    status: str
    score_e4: int
    parent_grade_e4: int
    centrality_e4: int
    question_score_e4: int
    feedback_e4: int
    ageing_e4: int
    wait_sec: int
    floor_e4: int
    admissible: bool
    gen_position: int
    queued_depth: int


@dataclass(frozen=True)
class EvaQueueRow:
    packet_id: str
    packet_ref: str
    status: str
    missing_mandatory_fields: int
    missing_optional_fields: int
    question_context_count: int
    eva_priority_e4: int


def _select(session: Session, sql: str, now: datetime | None) -> list[dict[str, Any]]:
    dialect = session.get_bind().dialect.name
    if now is None:
        stmt = text(sql.format(now="CURRENT_TIMESTAMP"))
        params: dict[str, Any] = {}
    else:
        expr = "CAST(:now AS TIMESTAMP WITH TIME ZONE)" if dialect == "postgresql" else ":now"
        stmt = text(sql.format(now=expr)).bindparams(bindparam("now", type_=UTCDateTime()))
        params = {"now": now}
    session.flush()
    return [dict(r) for r in session.execute(stmt, params).mappings()]


def _sql(builder: Callable[[str, str], str], session: Session) -> str:
    dialect = session.get_bind().dialect.name
    return (
        builder(dialect, "{now}").replace("{", "{{").replace("}", "}}").replace("{{now}}", "{now}")
    )


def gen_queue_rows(session: Session, now: datetime | None = None) -> list[GenQueueRow]:
    """``v_gen_queue`` at ``now`` (defaults to the database clock), in position order."""
    rows = _select(session, _sql(genqueue.gen_queue_select, session), now)
    result = [
        GenQueueRow(
            packet_id=r["packet_id"],
            packet_ref=r["packet_ref"],
            status=r["status"],
            score_e4=int(r["score_e4"]),
            parent_grade_e4=int(r["parent_grade_e4"]),
            centrality_e4=int(r["centrality_e4"]),
            question_score_e4=int(r["question_score_e4"]),
            feedback_e4=int(r["feedback_e4"]),
            ageing_e4=int(r["ageing_e4"]),
            wait_sec=int(r["wait_sec"]),
            floor_e4=int(r["floor_e4"]),
            admissible=bool(r["admissible"]),
            gen_position=int(r["gen_position"]),
            queued_depth=int(r["queued_depth"]),
        )
        for r in rows
    ]
    return sorted(result, key=lambda r: (r.gen_position, r.packet_id))


def eva_queue_rows(session: Session, now: datetime | None = None) -> list[EvaQueueRow]:
    """``v_eva_queue`` at ``now``: highest evaQueue priority first (OQ5)."""
    rows = _select(session, _sql(genqueue.eva_queue_select, session), now)
    rows.sort(key=lambda r: (-int(r["eva_priority_e4"]), int(r["ready_epoch"]), r["packet_id"]))
    return [
        EvaQueueRow(
            packet_id=r["packet_id"],
            packet_ref=r["packet_ref"],
            status=r["status"],
            missing_mandatory_fields=int(r["missing_mandatory_fields"]),
            missing_optional_fields=int(r["missing_optional_fields"]),
            question_context_count=int(r["question_context_count"]),
            eva_priority_e4=int(r["eva_priority_e4"]),
        )
        for r in rows
    ]


def scored_rows(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """``v_packet_queue_score`` at ``now``: every developing or running packet."""
    return _select(session, _sql(genqueue.queue_score_select, session), now)


# --------------------------------------------------------------------------- epochs (OQ31)


def _edges(session: Session) -> list[tuple[str, str]]:
    revoked = select(Revocation.target_id).where(Revocation.target_type == "dependency")
    return [
        (a, b)
        for a, b in session.execute(
            select(PacketDependency.packet_id, PacketDependency.depends_on_id).where(
                PacketDependency.id.not_in(revoked)
            )
        )
    ]


def lockout_set(
    zone: set[str], packets: dict[str, dict[str, Any]], edges: list[tuple[str, str]]
) -> set[str]:
    """R6 OQ3: positions 0-2 plus packets sharing their lineage (``root_id``), linked by a hard
    dependency in either direction, or writing the same non-null repo path."""

    def key(pid: str) -> str:
        row = packets.get(pid, {})
        return row.get("root_id") or pid

    locked = set(zone)
    keys = {key(z) for z in zone} | set(zone)
    paths = {
        packets[z]["repo_target_path"] for z in zone if packets.get(z, {}).get("repo_target_path")
    }
    for pid, row in packets.items():
        if key(pid) in keys or (row.get("repo_target_path") and row["repo_target_path"] in paths):
            locked.add(pid)
    for a, b in edges:  # a depends on b (b may be a lineage key)
        a_hit = a in zone
        b_hit = b in keys
        if a_hit:
            locked |= {pid for pid in packets if pid == b or key(pid) == b}
        if b_hit:
            locked.add(a)
    return locked


def write_epoch_snapshot(
    session: Session, cause: str, *, now: datetime | None = None, actor: Actor = KERNEL
) -> int:
    """Freeze genQueue positions, scores and the lockout into a new epoch (evented)."""
    scored = {r["packet_id"]: r for r in scored_rows(session, now)}
    positions = {r.packet_id: r for r in gen_queue_rows(session, now)}
    zone = {pid for pid, r in positions.items() if r.gen_position <= 2}
    locked = lockout_set(zone, scored, _edges(session))
    epoch = allocate(session, "queue_epoch")
    for pid, row in sorted(scored.items()):
        g = positions.get(pid)
        session.add(
            QueueEpochSnapshot(
                id=new_id(),
                epoch_id=epoch,
                packet_id=pid,
                status=row["status"],
                queue_pos=g.gen_position if g is not None else None,
                is_locked_out=pid in locked,
                score_e4=int(row["score_e4"]),
                cause=cause,
            )
        )
    record_event(
        session,
        actor=actor,
        action="queue.epoch",
        entity_type="queue_epoch",
        entity_id=str(epoch),
        after={
            "cause": cause,
            "positions": {positions[p].packet_ref: positions[p].gen_position for p in positions},
            "locked_out": sorted(scored[p]["packet_ref"] for p in locked if p in scored),
        },
    )
    session.flush()
    return epoch


def latest_epoch(session: Session) -> int | None:
    session.flush()
    return session.scalar(select(func.max(QueueEpochSnapshot.epoch_id)))


def is_locked_out(session: Session, packet_id: str) -> bool:
    """R6 OQ31: the lockout as of the newest epoch snapshot (no snapshot → no lockout)."""
    epoch = latest_epoch(session)
    if epoch is None:
        return False
    return bool(
        session.scalar(
            select(QueueEpochSnapshot.is_locked_out).where(
                QueueEpochSnapshot.epoch_id == epoch, QueueEpochSnapshot.packet_id == packet_id
            )
        )
    )


# --------------------------------------------------------------------------- feedback (OQ9)


def add_feedback(
    session: Session, packet_ref: str, kind: str, *, actor: Actor, reason: str
) -> FeedbackEvent:
    """A human queue weight (BOOST / NUDGE / DEMOTE / PIN); the latest per (packet, actor) wins
    and decays in SQL. Only users give feedback."""
    from orchestrator.repository import get_packet

    if not actor.is_user:
        raise PermissionError("only a user gives queue feedback")
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"feedback kind must be one of {FEEDBACK_KINDS}")
    packet = get_packet(session, packet_ref)
    row = FeedbackEvent(
        id=new_id(), packet_id=packet.id, actor=str(actor), kind=kind, reason=reason
    )
    session.add(row)
    record_event(
        session,
        actor=actor,
        action="queue.feedback",
        entity_type="feedback_event",
        entity_id=row.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"kind": kind},
        reason=reason,
    )
    session.flush()
    return row


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


# --------------------------------------------------------------------------- admission (OQ7, OQ12)


@dataclass
class TickReport:
    admitted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    epoch_id: int | None = None


def admission_tick(
    session: Session, *, now: datetime | None = None, actor: Actor = KERNEL
) -> TickReport:
    """One admission tick: drain, demote, promote, snapshot. Each move is a guarded UPDATE on
    (status, gate revision, input hash) after the readiness predicate, so a concurrent change
    loses the race instead of being overwritten."""
    from orchestrator.evaluation.readiness import readiness
    from orchestrator.gates import guarded_status_update
    from orchestrator.revisions import drain_outbox

    report = TickReport()
    drain_outbox(session, actor=actor)
    low_depth = get_int(session, "admission.low_depth", 3)
    floor = get_int(session, "admission.floor_e4", 3500)
    low = get_int(session, "admission.low_floor_e4", 2500)
    bypass = get_int(session, "admission.bypass_wait_sec", 7200)
    rows = gen_queue_rows(session, now)
    depth = sum(1 for r in rows if r.status == "queued")

    def admissible(row: GenQueueRow, depth: int) -> bool:
        threshold = low if depth < low_depth else floor
        return row.wait_sec >= bypass or row.score_e4 >= threshold

    # OQ12: queued → ready only by the score falling below the floor; lowest score first
    for row in reversed(rows):
        # hysteresis: a queued packet is measured against the depth without itself, the same
        # depth it was admitted at, so the low floor cannot flip it out and back in each tick
        if row.status != "queued" or admissible(row, depth - 1):
            continue
        packet = session.get(Packet, row.packet_id)
        assert packet is not None
        session.refresh(packet)
        if guarded_status_update(
            session,
            packet,
            expected=PacketStatus.QUEUED,
            observed_gate=packet.gate_revision,
            values={"status": PacketStatus.READY, "updated_at": utcnow()},
        ):
            depth -= 1
            report.demoted.append(packet.ref)
            _admission_event(
                session,
                packet,
                actor,
                "queued",
                "ready",
                f"score {row.score_e4} fell below the floor",
            )
    for row in gen_queue_rows(session, now):
        if row.status != "ready" or not admissible(row, depth):
            continue
        packet = session.get(Packet, row.packet_id)
        assert packet is not None
        session.refresh(packet)
        observed = packet.gate_revision
        verdict = readiness(session, packet)
        if not verdict.ready:
            continue
        if not guarded_status_update(
            session,
            packet,
            expected=PacketStatus.READY,
            observed_gate=observed,
            values={"status": PacketStatus.QUEUED, "updated_at": utcnow()},
        ):
            continue  # lost a race; the next tick sees the new state
        depth += 1
        threshold = low if depth - 1 < low_depth else floor
        cls = "bypass" if row.score_e4 < threshold else "relational"
        _admission_event(
            session,
            packet,
            actor,
            "ready",
            "queued",
            f"admitted ({cls}: score {row.score_e4}, floor {threshold}, wait {row.wait_sec}s)",
        )
        admission = QueueAdmission(
            id=new_id(), packet_id=packet.id, packet_version=packet.version, admission_class=cls
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
                "class": cls,
                "score_e4": row.score_e4,
                "floor_e4": threshold,
                "wait_sec": row.wait_sec,
                "waived": verdict.waived,
                "gate_revision": observed,
            },
        )
        session.flush()
        report.admitted.append(packet.ref)
    report.epoch_id = write_epoch_snapshot(session, "admission", now=now, actor=actor)
    return report


def admit_next(
    session: Session, *, now: datetime | None = None, actor: Actor = KERNEL
) -> Packet | None:
    """Run one admission tick; return the first packet it admitted (or ``None``)."""
    from orchestrator.repository import get_packet

    report = admission_tick(session, now=now, actor=actor)
    return get_packet(session, report.admitted[0]) if report.admitted else None


def next_dispatch(session: Session) -> str | None:
    """R6 OQ25: the packet the kernel should claim next, read from the newest epoch snapshot."""
    epoch = latest_epoch(session)
    if epoch is None:
        return None
    rows = session.execute(
        select(QueueEpochSnapshot, Packet)
        .join(Packet, Packet.id == QueueEpochSnapshot.packet_id)
        .where(QueueEpochSnapshot.epoch_id == epoch, QueueEpochSnapshot.status == "queued")
        .order_by(QueueEpochSnapshot.queue_pos, QueueEpochSnapshot.packet_id)
    ).all()
    if not rows:
        return None
    dispatched = session.scalar(select(func.count()).select_from(GenerationRun)) or 0
    every = get_int(session, "dispatch.oldest_every", 5)
    if every > 0 and (dispatched + 1) % every == 0:
        oldest = min(rows, key=lambda r: (r[1].ready_since or r[1].created_at, r[1].id))
        return oldest[1].ref
    leader_snap, leader = rows[0]
    previous = session.scalar(
        select(QueueEpochSnapshot)
        .where(
            QueueEpochSnapshot.epoch_id < epoch,
            QueueEpochSnapshot.status == "queued",
            QueueEpochSnapshot.queue_pos.is_not(None),
        )
        .order_by(QueueEpochSnapshot.epoch_id.desc(), QueueEpochSnapshot.queue_pos)
        .limit(1)
    )
    margin = get_int(session, "dispatch.incumbency_margin_e4", 250)
    if previous is not None and previous.packet_id != leader.id:
        for snap, packet in rows:
            if packet.id == previous.packet_id and (snap.score_e4 or 0) + margin > (
                leader_snap.score_e4 or 0
            ):
                return packet.ref
    return leader.ref


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
        claim_content_revision=packet.content_revision,  # R6 OQ20
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
    write_epoch_snapshot(session, "dispatch", now=now, actor=actor)  # R6 OQ31
    return run
