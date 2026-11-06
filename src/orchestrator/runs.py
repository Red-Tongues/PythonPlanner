"""Durable generation runs (M3 C24–C27, D8 with errata E2/E3/E7/E8, D10 Q5).

Every run state change goes through ``move_run``: the M3 run table (``orchestrator.transitions``),
then one compare-and-set UPDATE on (id, state, fencing token), then an event. The database
database triggers (``db/ddl.py``) enforce the same table and the frozen bindings.

* **Bindings** (``binding_mismatch``): a run may act only while its packet row is still the head
  version, is owned by this run (``packets.active_run_id``, E7), carries this run's fencing token
  (C26), has the same gate revision (C25) and the same, still-current context fingerprint H.
  Otherwise the run is stale.
* **Dispatch guard** (``authorise_dispatch``, E3) — one guard for the first dispatch, every retry
  and every exit-3 resume: kernel lock fenced, bindings and lease revalidated, every governing
  scope authorised against pauses (serialised with ``control.pause``, C26), capacity reserved
  (C36), then the operation ID and the immutable attempt row are written. All of it commits
  *before* any adapter effect, and the attempt's ``operation_id`` is unique, so a recovery can
  recognise an effect that already happened and nothing is dispatched twice.
* **Outcomes** (``record_outcome``) are append-only and recorded once per attempt. A result for
  an obsolete fencing token never revives anything.
* **Acceptance and publication** (``accept_run``, ``assert_publishable``) re-check the bindings;
  ``accepted`` becomes final once the commit is recorded (R4); before that a mismatch stales it
  (E8).
* **Staleness** (``stale_run``, C25 as amended by V2/E7): only the run becomes stale; the packet
  returns to ``evaluating_pre`` only if it is still ``running`` on this run's version with this
  run's F, and no hold, block, pause or confirmation applies.
* **Parked runs** (exit 3): approval follows R1 (run G = packet G, then both + 1) and resumes the
  same run through the E3 guard exactly once; rejection holds the packet and bumps both G
  together (D10 Q5), so the run stays parked and matched until the owner cancels it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from orchestrator.actors import Actor
from orchestrator.budgets import Demand, reserve
from orchestrator.contracts import (
    AttemptOutcome,
    Channel,
    ConfirmationStatus,
    GenerationRunState,
    OverrideKind,
    PacketStatus,
    ScopeStatus,
    Stage,
)
from orchestrator.control import FencedOut, KernelLock, authorise_scopes, scope_status
from orchestrator.db.models import (
    ChannelRun,
    ChannelRunOutcome,
    Confirmation,
    GenerationRun,
    Objective,
    Packet,
    Project,
    new_id,
    utcnow,
)
from orchestrator.db.refs import allocate
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event
from orchestrator.transitions import RUN_ACTIVE, check_packet, check_run

log = logging.getLogger(__name__)

KERNEL = Actor.system("kernel")
R = GenerationRunState
S = PacketStatus
PREFIX = {
    Channel.QUICKFIRE_EVALUATION: "QE",
    Channel.LOCAL_GENERATION: "LG",
    Channel.WORKBENCH_PRODUCTION: "WP",
}


class RunError(OrchestratorError):
    """A run operation is not allowed in the current state."""


class StaleRun(RunError):
    """The run's bindings no longer hold; it was (or must be) made stale."""


class LostRace(RunError):
    """A compare-and-set matched no row: another writer changed the run or packet first."""


# --------------------------------------------------------------------------- moves


def _run_snapshot(run: GenerationRun) -> dict[str, Any]:
    return {
        "state": GenerationRunState(run.state).value,
        "fencing_token": run.fencing_token,
        "gate_revision": run.gate_revision,
        "attempts": run.attempts,
    }


def move_run(
    session: Session,
    run: GenerationRun,
    target: GenerationRunState,
    actor: Actor,
    reason: str | None = None,
    **values: Any,
) -> None:
    """Apply one row of the run table with a compare-and-set UPDATE and an event."""
    source = GenerationRunState(run.state)
    check_run(source, target, actor)
    session.flush()
    before = _run_snapshot(run)
    table = GenerationRun.__table__
    changes = {"state": target.value, "updated_at": utcnow(), **values}
    if target in (R.FAILED, R.STALE) and "finished_at" not in changes:
        changes["finished_at"] = utcnow()
    result = session.connection().execute(
        update(table)
        .where(
            table.c.id == run.id,
            table.c.state == source.value,
            (table.c.fencing_token == run.fencing_token)
            if run.fencing_token is not None
            else table.c.fencing_token.is_(None),
        )
        .values(**changes)
    )
    if result.rowcount != 1:
        raise LostRace(f"run {run.id}: {source.value} → {target.value} lost a race")
    for attr, value in changes.items():
        set_committed_value(run, attr, GenerationRunState(value) if attr == "state" else value)
    record_event(
        session,
        actor=actor,
        action="run.transition",
        entity_type="generation_run",
        entity_id=run.id,
        entity_version=run.packet_version,
        before=before,
        after=_run_snapshot(run),
        reason=reason,
    )
    session.flush()


def _packet_cas(
    session: Session,
    packet: Packet,
    target: PacketStatus,
    actor: Actor,
    reason: str,
    *,
    where: Sequence[Any] = (),
    **values: Any,
) -> None:
    """A packet transition (table-checked) as a compare-and-set on status plus ``where``."""
    source = PacketStatus(packet.status)
    check_packet(source, target, actor)
    session.flush()
    table = Packet.__table__
    changes = {"status": target.value, "updated_at": utcnow(), **values}
    result = session.connection().execute(
        update(table)
        .where(table.c.id == packet.id, table.c.status == source.value, *where)
        .values(**changes)
    )
    if result.rowcount != 1:
        raise LostRace(f"{packet.ref}: {source.value} → {target.value} lost a race")
    for attr, value in changes.items():
        set_committed_value(packet, attr, PacketStatus(value) if attr == "status" else value)
    record_event(
        session,
        actor=actor,
        action="packet.transition",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"status": source.value},
        after={
            "status": target.value,
            **{
                k: v.isoformat() if isinstance(v, datetime) else v
                for k, v in values.items()
                if k != "updated_at"
            },
        },
        reason=reason,
    )
    session.flush()


# --------------------------------------------------------------------------- bindings


def binding_mismatch(
    session: Session, run: GenerationRun, *, now: datetime | None = None, lease: bool = True
) -> str | None:
    """Why ``run`` may no longer act, or ``None`` when every binding holds (C26, E7; M3a R6 Q7,
    OQ19). Running generations are never overridden by calculations: a ``gate_revision``,
    ``content_revision`` or ``input_hash`` change, an evaPacket or a soft HOLD does **not**
    stale a run. What still stops it: a superseded fencing token or ownership, an expired
    lease, an owner close (locked) or BLOCK, and a hard GLOBAL_PAUSE (lease revoked)."""
    from orchestrator.governance import active_overrides

    session.refresh(run)
    packet = session.get(Packet, run.packet_id)
    if packet is None:
        return "packet missing"
    session.refresh(packet)
    if run.fencing_token is None:
        return "legacy run without a fencing token"
    if session.scalar(select(Packet.id).where(Packet.supersedes_id == packet.id)) is not None:
        return "packet version superseded"
    if packet.locked:
        return f"packet is {PacketStatus(packet.status).value}"
    if packet.active_run_id != run.id:
        return "packet is owned by another run"
    if packet.fencing_token != run.fencing_token:
        return f"fencing token {run.fencing_token} is obsolete (packet has {packet.fencing_token})"
    if PacketStatus(packet.status) is S.BLOCKED or any(
        OverrideKind(o.kind) is OverrideKind.BLOCK for o in active_overrides(session, packet)
    ):
        return "owner BLOCK"
    if scope_status(session, "global", "*") is ScopeStatus.PAUSED:
        return "global pause (hard halt: lease revoked)"
    if lease and run.lease_expires_at is not None and run.lease_expires_at < (now or utcnow()):
        return "lease expired"
    return None


def _binding_conditions(run: GenerationRun, status: PacketStatus) -> list[Any]:
    """The packet-row conditions every binding-dependent run write re-checks atomically."""
    table = Packet.__table__
    successor = table.alias("successor")
    return [
        table.c.id == run.packet_id,
        table.c.version == run.packet_version,
        table.c.status == status.value,
        table.c.locked.is_(False),
        table.c.active_run_id == run.id,
        table.c.fencing_token == run.fencing_token,
        ~select(successor.c.id).where(successor.c.supersedes_id == table.c.id).exists(),
    ]


def guard_bindings(
    session: Session, run: GenerationRun, *, status: PacketStatus = PacketStatus.RUNNING
) -> None:
    """MR1: make the binding check part of the write. One compare-and-set on the packet row in
    the caller's transaction re-checks version, status, lock, ownership (active run and F) and
    headship (M3a, R6 OQ19: not G or H); it also takes the row lock (PostgreSQL) or the write
    lock (SQLite), so an ownership change committed after ``binding_mismatch`` read the row makes
    it match nothing.
    Raises ``StaleRun``; the caller rolls back (or records a stale disposition)."""
    if run.fencing_token is None or run.gate_revision is None:
        raise StaleRun(f"run {run.id}: legacy run without a fencing token")
    session.flush()
    table = Packet.__table__
    result = session.connection().execute(
        update(table)
        .where(*_binding_conditions(run, status))
        .values(gate_revision=table.c.gate_revision)  # a no-op write that locks the row
    )
    if result.rowcount != 1:
        raise StaleRun(f"run {run.id}: the packet changed under it (bindings no longer hold)")


def _restricted(session: Session, packet: Packet) -> str | None:
    """A hold, block or confirmation that keeps a packet where it is (C25 / V2 / E7). A pause no
    longer does: R6 OQ19 makes a hard pause revoke the lease (``running → queued``)."""
    from orchestrator.governance import active_overrides

    for o in active_overrides(session, packet):
        if OverrideKind(o.kind) in (OverrideKind.BLOCK, OverrideKind.HOLD):
            return f"{OverrideKind(o.kind).value} override"
    pending = session.scalar(
        select(Confirmation.id).where(
            Confirmation.entity_id.in_([packet.id, packet.active_run_id or ""]),
            Confirmation.status == ConfirmationStatus.PENDING,
        )
    )
    return "a confirmation is pending" if pending else None


def packet_scopes(session: Session, packet: Packet) -> list[tuple[str, str]]:
    """Every governance scope a dispatch for ``packet`` must be authorised against (C26)."""
    project = session.get(Project, packet.project_id)
    scopes = [("global", "*"), ("packet", packet.ref)]
    if project is not None:
        scopes.append(("project", project.ref))
    if packet.objective_id:
        objective = session.get(Objective, packet.objective_id)
        if objective is not None:
            scopes.append(("objective", objective.ref))
    root = session.get(Packet, packet.root_id) if packet.root_id else packet
    if root is not None:
        scopes.append(("branch", root.ref))
    if packet.channel:
        scopes.append(("channel", Channel(packet.channel).value))
    return scopes


# --------------------------------------------------------------------------- staleness


#: where an owned, unrestricted packet goes when its run becomes stale (M3a, R6 OQ12/OQ19: a
#: running genPacket is never demoted to evaluating_pre; its lease is revoked → queued)
_RECOVER_TO: dict[PacketStatus, PacketStatus] = {
    S.RUNNING: S.QUEUED,
    S.AWAITING_CONFIRMATION: S.EVALUATING_PRE,
}


def stale_run(session: Session, run: GenerationRun, reason: str, *, actor: Actor = KERNEL) -> None:
    """Make ``run`` stale (evented). The packet returns to the queue (``running → queued``; a
    parked one to ``evaluating_pre``) only when it is still owned by this run with this run's F
    and nothing restricts it (E7); otherwise its state and restrictions are kept. Terminal
    packets are never touched."""
    move_run(session, run, R.STALE, actor, reason, stale_reason=reason)
    _retire_parked_confirmations(session, run, actor, reason)
    packet = session.get(Packet, run.packet_id)
    if packet is None or packet.locked:
        return
    session.refresh(packet)
    owned = packet.active_run_id == run.id and packet.fencing_token == run.fencing_token
    if not owned:
        return
    table = Packet.__table__
    owner = [table.c.active_run_id == run.id, table.c.fencing_token == run.fencing_token]
    is_head = session.scalar(select(Packet.id).where(Packet.supersedes_id == packet.id)) is None
    status = PacketStatus(packet.status)
    if (
        status in (S.RUNNING, S.AWAITING_CONFIRMATION)
        and is_head
        and _restricted(session, packet) is None
    ):
        _packet_cas(
            session,
            packet,
            _RECOVER_TO[status],
            actor,
            f"run {run.id} stale: {reason}",
            where=owner,
            active_run_id=None,
            recovery_pending=False,
            claimed_by=None,
            lease_expires_at=None,
        )
    elif status in (S.RUNNING, S.AWAITING_CONFIRMATION) and is_head:
        result = session.connection().execute(
            update(table)
            .where(table.c.id == packet.id, table.c.status == status.value, *owner)
            .values(
                recovery_pending=True,
                claimed_by=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=utcnow(),
            )
        )
        if result.rowcount != 1:
            raise LostRace(f"{packet.ref}: deferred recovery lost ownership")
        set_committed_value(packet, "recovery_pending", True)
        set_committed_value(packet, "claimed_by", None)
        set_committed_value(packet, "lease_expires_at", None)
        set_committed_value(packet, "heartbeat_at", None)
        record_event(
            session,
            actor=actor,
            action="packet.recovery_deferred",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            before={"active_run_id": run.id, "status": status.value},
            after={"active_run_id": run.id, "status": status.value, "recovery_pending": True},
            reason=f"run {run.id} stale: {reason}; recovery waits for restrictions to clear",
        )
        session.flush()
    else:  # keep state and restrictions; only release the ownership (evented)
        result = session.connection().execute(
            update(table)
            .where(table.c.id == packet.id, *owner)
            .values(active_run_id=None, recovery_pending=False)
        )
        if result.rowcount != 1:
            # R4 audit J2: a concurrent writer released or replaced the ownership after we read
            # it. The release is idempotent: its goal (no packet pointing at this stale run) is
            # already met, so record nothing rather than an event for a change we did not make.
            # We do not raise LostRace: that would roll back the run's own audited stale
            # disposition (MR9), which is still correct. Raise only if this run still owns it.
            session.refresh(packet)
            if packet.active_run_id == run.id and packet.fencing_token == run.fencing_token:
                raise LostRace(f"{packet.ref}: ownership release matched no row")
            log.info("%s: run %s already released by another writer", packet.ref, run.id)
            return
        set_committed_value(packet, "active_run_id", None)
        set_committed_value(packet, "recovery_pending", False)
        record_event(
            session,
            actor=actor,
            action="packet.run_released",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            before={"active_run_id": run.id},
            after={"active_run_id": None, "status": PacketStatus(packet.status).value},
            reason=f"run {run.id} stale: {reason}; packet state kept",
        )
        session.flush()


def _retire_parked_confirmations(
    session: Session, run: GenerationRun, actor: Actor, reason: str
) -> None:
    from orchestrator.confirmations import decide_once

    confirmations = session.scalars(
        select(Confirmation).where(
            Confirmation.entity_type == "generation_run",
            Confirmation.entity_id == run.id,
            Confirmation.status == ConfirmationStatus.PENDING,
        )
    ).all()
    for confirmation in confirmations:
        decide_once(session, confirmation, ConfirmationStatus.RETIRED, actor, reason)
        record_event(
            session,
            actor=actor,
            action="confirmation.retired",
            entity_type="confirmation",
            entity_id=confirmation.id,
            entity_ref=confirmation.entity_ref,
            entity_version=confirmation.entity_version,
            before={"status": ConfirmationStatus.PENDING.value},
            after={"status": ConfirmationStatus.RETIRED.value, "run": run.id},
            reason=f"parked run became stale: {reason}",
        )
    if confirmations:
        session.flush()


def _recover_deferred(session: Session, packet: Packet, run: GenerationRun, actor: Actor) -> bool:
    if (
        not packet.recovery_pending
        or packet.locked
        or PacketStatus(packet.status) not in (S.RUNNING, S.AWAITING_CONFIRMATION)
        or packet.active_run_id != run.id
        or packet.fencing_token != run.fencing_token
        or session.scalar(select(Packet.id).where(Packet.supersedes_id == packet.id)) is not None
        or _restricted(session, packet) is not None
    ):
        return False
    _packet_cas(
        session,
        packet,
        _RECOVER_TO[PacketStatus(packet.status)],
        actor,
        f"deferred recovery after stale run {run.id}",
        where=[
            Packet.__table__.c.active_run_id == run.id,
            Packet.__table__.c.fencing_token == run.fencing_token,
            Packet.__table__.c.recovery_pending.is_(True),
        ],
        active_run_id=None,
        recovery_pending=False,
        claimed_by=None,
        lease_expires_at=None,
        heartbeat_at=None,
    )
    return True


def reconcile_runs(session: Session, *, now: datetime | None = None) -> list[str]:
    """Stale every active run whose bindings no longer hold (startup and every tick). A parked
    run has no lease to expire: it waits for its owner, but any other governance change still
    stales it (E2)."""
    staled: list[str] = []
    # R4 audit J3: a fixed (packet_id, id) order, so two kernels lock rows in the same order
    for run in session.scalars(
        select(GenerationRun)
        .where(GenerationRun.state.in_([s.value for s in RUN_ACTIVE]))
        .order_by(GenerationRun.packet_id, GenerationRun.id)
    ).all():
        if run.state == R.ACCEPTED and run.commit_sha is not None:
            continue
        parked = GenerationRunState(run.state) is R.AWAITING_CONFIRMATION
        why = binding_mismatch(session, run, now=now, lease=not parked)
        if why is not None:
            stale_run(session, run, why)
            staled.append(run.id)
    deferred = session.scalars(
        select(GenerationRun)
        .join(Packet, Packet.active_run_id == GenerationRun.id)
        .where(
            GenerationRun.state == R.STALE.value,
            Packet.recovery_pending.is_(True),
        )
        .order_by(GenerationRun.packet_id, GenerationRun.id)
    ).all()
    for run in deferred:
        packet = session.get(Packet, run.packet_id)
        if packet is not None:
            _recover_deferred(session, packet, run, KERNEL)
    return staled


# --------------------------------------------------------------------------- authority (MR2, MR3)


def _require_fence(lock: KernelLock | None, session: Session, now: datetime | None) -> None:
    """Effect-authorising kernel writes need the kernel lock, proven in this transaction."""
    if lock is None:
        raise FencedOut("a kernel write needs the kernel lock (none was given)")
    lock.fence(session, now=now)


def _check_approval(
    session: Session, run: GenerationRun, approval: Confirmation | None, *, parked: bool
) -> None:
    """A user dispatch is the resume of a parked run, carried by its just-approved confirmation,
    while the packet is ``running`` with no HOLD or BLOCK."""
    from orchestrator.governance import active_overrides

    if not parked or approval is None:
        raise RunError(f"run {run.id}: a user may only resume a parked run via approve_run")
    if (
        approval.entity_type != "generation_run"
        or approval.entity_id != run.id
        or approval.entity_version != run.packet_version
        or ConfirmationStatus(approval.status) is not ConfirmationStatus.APPROVED
    ):
        raise RunError(f"run {run.id}: the approval is not a decided approval of this run")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    if PacketStatus(packet.status) is not S.RUNNING:
        raise RunError(f"{packet.ref} is {PacketStatus(packet.status).value}, not running")
    for override in active_overrides(session, packet):
        if OverrideKind(override.kind) in (OverrideKind.BLOCK, OverrideKind.HOLD):
            raise RunError(f"{packet.ref} has an active {OverrideKind(override.kind).value}")


# --------------------------------------------------------------------------- dispatch (E3)

_DISPATCH_FROM = {
    R.PLANNED: R.DISPATCHED,
    R.AWAITING_CONFIRMATION: R.DISPATCHED,  # exit-3 resume, after R1
    R.AWAITING_RESULT: R.AWAITING_RESULT,  # exit 1/4 retry
    R.VALIDATING: R.AWAITING_RESULT,  # validation retry
    R.DISPATCHED: R.DISPATCHED,  # deadline retry (E11)
}


def _retry_is_eligible(
    session: Session, run: GenerationRun, source: GenerationRunState, now: datetime
) -> bool:
    if source is R.DISPATCHED:
        attempt = session.scalar(
            select(ChannelRun)
            .where(ChannelRun.generation_run_id == run.id)
            .order_by(ChannelRun.attempt.desc())
            .limit(1)
        )
        deadline = attempt.deadline_at if attempt is not None else run.deadline_at
        return deadline is not None and deadline <= now

    attempt = session.scalar(
        select(ChannelRun)
        .where(ChannelRun.generation_run_id == run.id)
        .order_by(ChannelRun.attempt.desc())
        .limit(1)
    )
    if attempt is None:
        return False
    outcome = session.scalar(
        select(ChannelRunOutcome.outcome).where(ChannelRunOutcome.channel_run_id == attempt.id)
    )
    if outcome in (AttemptOutcome.FAILED, AttemptOutcome.INVALID, AttemptOutcome.TIMEOUT):
        return True
    deadline = attempt.deadline_at or run.deadline_at
    return deadline is not None and deadline <= now


def authorise_dispatch(
    session: Session,
    run: GenerationRun,
    *,
    actor: Actor = KERNEL,
    now: datetime | None = None,
    lock: KernelLock | None = None,
    demands: Sequence[Demand] = (),
    contract: str = "generation@1",
    before_scopes: Any = None,
    approval: Confirmation | None = None,
    _resume_lease: datetime | None = None,
) -> ChannelRun:
    """The single dispatch guard (E3). Returns the new immutable attempt row; the caller commits
    and only then performs the effect under ``attempt.operation_id``.

    Raises ``StaleRun`` (bindings), ``control.DispatchRefused`` (paused), ``FencedOut``
    (kernel lock) or ``budgets.BudgetExhausted``; on any of these the caller rolls back and the
    packet and run are unchanged. ``before_scopes`` is a test seam.

    MR2/MR3: a ``system`` actor must hold the kernel lock (``lock`` is fenced; none →
    ``FencedOut``). A ``user`` actor dispatches only through ``approve_run``, which passes the
    run-bound confirmation it has just approved (``approval``); a parked run can be dispatched
    no other way.
    """
    now = now or utcnow()
    source = GenerationRunState(run.state)
    if source not in _DISPATCH_FROM:
        raise RunError(f"run {run.id} cannot be dispatched from {source.value}")
    if run.channel is None:  # fail closed: never guess a channel for an unrouted packet
        raise RunError(f"run {run.id} has no channel (the packet was not routed)")
    parked = source is R.AWAITING_CONFIRMATION
    if _resume_lease is not None and not parked:
        raise RunError(f"run {run.id}: only an approved resume can renew its lease")
    if actor.is_user:
        _check_approval(session, run, approval, parked=parked)
    else:
        _require_fence(lock, session, now)
        if parked or approval is not None:
            raise RunError(f"run {run.id}: a parked run resumes only through approve_run")
    chargeable_attempts = run.attempts + (0 if parked else 1)
    if run.max_attempts is not None and chargeable_attempts > run.max_attempts:
        raise RunError(f"run {run.id} has used all {run.max_attempts} attempts")
    why = binding_mismatch(session, run, now=now, lease=not parked)
    if why is not None:
        raise StaleRun(f"run {run.id}: {why}")
    if source in (R.DISPATCHED, R.AWAITING_RESULT, R.VALIDATING) and not _retry_is_eligible(
        session, run, source, now
    ):
        raise RunError(f"run {run.id}: the previous dispatch is not retry-eligible")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    if before_scopes is not None:
        before_scopes()
    guard_bindings(session, run)  # MR1: the check and the write are one statement
    authorise_scopes(session, packet_scopes(session, packet))
    attempt_number = (
        int(
            session.scalar(
                select(func.max(ChannelRun.attempt)).where(ChannelRun.generation_run_id == run.id)
            )
            or 0
        )
        + 1
    )
    operation_id = f"{run.id}:{attempt_number}"
    if demands:
        reserve(
            session,
            operation_id,
            list(demands),
            actor=actor,
            packet_id=packet.id,
            generation_run_id=run.id,
        )
    channel = Channel(run.channel)
    prefix = PREFIX[channel]
    attempt = ChannelRun(
        id=new_id(),
        run_id=f"{prefix}{allocate(session, f'run:{prefix}'):06d}",
        channel=channel,
        packet_id=packet.id,
        attempt=attempt_number,
        max_attempts=run.max_attempts or attempt_number,
        contract=contract,
        state="authorised",
        idempotency_key=operation_id,
        input_hash=run.input_hash,
        policy_revision=str(run.gate_revision),
        status="running",
        started_at=now,
        deadline_at=run.deadline_at,
        generation_run_id=run.id,
        operation_id=operation_id,
        fencing_token=run.fencing_token,
    )
    session.add(attempt)
    record_event(
        session,
        actor=actor,
        action="run.attempt_authorised",
        entity_type="channel_run",
        entity_id=attempt.id,
        entity_version=run.packet_version,
        after={
            "run": run.id,
            "console": (
                f"#{attempt.run_id} [dispatch {attempt_number}; "
                f"chargeable {chargeable_attempts}/{attempt.max_attempts}]"
            ),
            "operation_id": operation_id,
            "fencing_token": run.fencing_token,
        },
    )
    session.flush()
    move_run(
        session,
        run,
        _DISPATCH_FROM[source],
        actor,
        f"dispatch {attempt_number} authorised ({operation_id})",
        attempts=chargeable_attempts,
        heartbeat_at=now,
        **({"lease_expires_at": _resume_lease} if _resume_lease is not None else {}),
    )
    return attempt


# --------------------------------------------------------------------------- outcomes


def record_outcome(
    session: Session,
    attempt: ChannelRun,
    outcome: AttemptOutcome,
    *,
    actor: Actor = KERNEL,
    exit_code: int | None = None,
    error: str | None = None,
    output_hash: str | None = None,
    tokens_used: int | None = None,
) -> ChannelRunOutcome | None:
    """Record how one attempt ended, once. A duplicate report returns ``None`` and changes
    nothing; a report for an obsolete fencing token is recorded as ``stale`` evidence."""
    existing = session.scalar(
        select(ChannelRunOutcome).where(ChannelRunOutcome.channel_run_id == attempt.id)
    )
    if existing is not None:
        return None
    packet = session.get(Packet, attempt.packet_id) if attempt.packet_id else None
    if packet is not None:
        session.refresh(packet)
    fenced = packet is not None and packet.fencing_token != attempt.fencing_token
    row = ChannelRunOutcome(
        id=new_id(),
        channel_run_id=attempt.id,
        generation_run_id=attempt.generation_run_id,
        outcome=AttemptOutcome.STALE if fenced else outcome,
        exit_code=exit_code,
        error=("late result for an obsolete fencing token; " + (error or "")).strip("; ")
        if fenced
        else error,
        output_hash=output_hash,
        tokens_used=tokens_used,
    )
    session.add(row)
    record_event(
        session,
        actor=actor,
        action="run.attempt_outcome",
        entity_type="channel_run_outcome",
        entity_id=row.id,
        after={"attempt": attempt.run_id, "outcome": AttemptOutcome(row.outcome).value},
        reason=row.error,
    )
    session.flush()
    return row


def accept_run(
    session: Session,
    run: GenerationRun,
    manifest_hash: str,
    *,
    actor: Actor = KERNEL,
    now: datetime | None = None,
    lock: KernelLock | None = None,
) -> None:
    """``validating → accepted`` with the output's manifest hash M (C30), only while every
    binding holds; otherwise the run is made stale and ``StaleRun`` is raised. The kernel must
    hold the lock (MR2)."""
    _require_fence(lock, session, now)
    why = binding_mismatch(session, run, now=now)
    if why is not None:
        stale_run(session, run, why, actor=actor)
        raise StaleRun(f"run {run.id}: {why}")
    guard_bindings(session, run)
    move_run(session, run, R.ACCEPTED, actor, "checks passed", manifest_hash=manifest_hash)


def assert_publishable(
    session: Session,
    run: GenerationRun,
    *,
    lock: KernelLock,
    now: datetime | None = None,
    actor: Actor = KERNEL,
) -> None:
    """The fencing half of the publication guard (C26, E4, E8): the kernel lock is held, the
    run is accepted with no commit recorded, and every binding still holds. A fenced-out old
    owner raises ``FencedOut`` before anything is written. A binding mismatch stales the run."""
    lock.fence(session, now=now)
    if GenerationRunState(run.state) is not R.ACCEPTED or run.commit_sha is not None:
        raise RunError(f"run {run.id} is not awaiting publication")
    why = binding_mismatch(session, run, now=now)
    if why is not None:
        stale_run(session, run, why, actor=actor)
        raise StaleRun(f"run {run.id}: {why}")
    guard_bindings(session, run)  # MR1: held until the publication transaction commits


# --------------------------------------------------------------------------- parked runs (exit 3)


def _bump_pair(session: Session, packet: Packet, run: GenerationRun, cause: str, actor: Actor):
    """R1 as amended by R6 OQ19: a decision aimed at a parked run binds its confirmation (decided
    once, by CAS) and the fencing token — not the gate revision. Packet and run gate revisions
    each move by +1 in the same transaction (governance record); the packet-side UPDATE also
    re-checks every remaining binding (MR1)."""
    table, runs = Packet.__table__, GenerationRun.__table__
    observed = run.gate_revision
    if observed is None:
        raise StaleRun(f"run {run.id}: legacy run without a gate revision")
    a = session.connection().execute(
        update(table)
        .where(*_binding_conditions(run, PacketStatus.AWAITING_CONFIRMATION))
        .values(gate_revision=table.c.gate_revision + 1)
    )
    b = session.connection().execute(
        update(runs)
        .where(runs.c.id == run.id, runs.c.gate_revision == observed)
        .values(gate_revision=observed + 1)
    )
    if a.rowcount != 1 or b.rowcount != 1:
        raise LostRace(f"run {run.id}: the packet or run changed first")
    new_gate = (
        session.connection()
        .execute(select(table.c.gate_revision).where(table.c.id == packet.id))
        .scalar_one()
    )
    set_committed_value(packet, "gate_revision", new_gate)
    set_committed_value(run, "gate_revision", observed + 1)
    record_event(
        session,
        actor=actor,
        action="packet.gate_revision",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"gate_revision": new_gate, "run": run.id, "cause": cause},
        reason="decision aimed at this run (R1 / R6 OQ19): packet and run bumped together",
    )
    session.flush()


def park_run(
    session: Session,
    run: GenerationRun,
    *,
    lock: KernelLock | None = None,
    actor: Actor = KERNEL,
    now: datetime | None = None,
) -> Confirmation:
    """Exit 3: ``awaiting_result → awaiting_confirmation`` (no counters), the packet
    ``running → awaiting_confirmation`` and exactly one confirmation aimed at this run. The
    kernel must hold the lock (MR2)."""
    _require_fence(lock, session, now)
    why = binding_mismatch(session, run)
    if why is not None:
        stale_run(session, run, why, actor=actor)
        raise StaleRun(f"run {run.id}: {why}")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    guard_bindings(session, run)
    move_run(session, run, R.AWAITING_CONFIRMATION, actor, "exit 3: parked for the owner")
    _packet_cas(
        session,
        packet,
        S.AWAITING_CONFIRMATION,
        actor,
        "the workbench asked for a human decision (exit 3)",
        where=[Packet.__table__.c.active_run_id == run.id],
    )
    channel = Channel(run.channel) if run.channel else Channel.LOCAL_GENERATION
    confirmation = Confirmation(
        id=new_id(),
        stage=Stage(channel.value) if channel.value in Stage._value2member_map_ else Stage.ROUTING,
        entity_type="generation_run",
        entity_id=run.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        summary=f"{packet.ref} v{packet.version}: run parked (exit 3); approve to resume it",
        payload={"run": run.id, "fencing_token": run.fencing_token},
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
        after={"kind": "parked_run", "run": run.id},
    )
    session.flush()
    _bump_pair(session, packet, run, "parked-run confirmation created", actor)
    return confirmation


def _parked(session: Session, confirmation_id: str) -> tuple[Confirmation, GenerationRun, Packet]:
    confirmation = session.get(Confirmation, confirmation_id)
    if confirmation is None or confirmation.entity_type != "generation_run":
        raise RunError(f"{confirmation_id} is not a parked-run confirmation")
    session.refresh(confirmation)
    run = session.get(GenerationRun, confirmation.entity_id)
    assert run is not None
    session.refresh(run)
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    session.refresh(packet)
    return confirmation, run, packet


def approve_run(
    session: Session,
    confirmation_id: str,
    *,
    actor: Actor,
    now: datetime | None = None,
    demands: Sequence[Demand] = (),
    reason: str | None = None,
) -> ChannelRun | None:
    """Approve a parked run (R1, E3): decide the confirmation once, check ``run.G == packet.G``
    and bump both, revalidate every binding, then resume the *same* run through the dispatch
    guard. Recoverable refusals raise so the caller rolls back and the confirmation stays pending.
    A stale binding is committed as a stale run with a retired confirmation and returns ``None``.
    """
    from orchestrator.confirmations import ConfirmationError, decide_once

    if not actor.is_user:
        raise PermissionError("only a user may approve a parked run")
    from orchestrator.settings_store import get_int

    now = now or utcnow()
    confirmation, run, packet = _parked(session, confirmation_id)
    if ConfirmationStatus(confirmation.status) is not ConfirmationStatus.PENDING:
        raise ConfirmationError(f"confirmation {confirmation.id} is already {confirmation.status}")
    if GenerationRunState(run.state) is not R.AWAITING_CONFIRMATION:
        raise RunError(f"run {run.id} is {GenerationRunState(run.state).value}, not parked")
    why = binding_mismatch(session, run, now=now, lease=False)
    if why is not None:
        # MR9: retire *this* confirmation first, with the same once-only CAS a decision uses.
        # If a concurrent rejection (or approval) decided it after we read it, this raises
        # ConfirmationError and we roll back: a late approval must not stale a run that the
        # winning decision kept parked (its G moved with the packet's, so it reads as stale).
        decide_once(session, confirmation, ConfirmationStatus.RETIRED, KERNEL, why)
        record_event(
            session,
            actor=KERNEL,
            action="confirmation.retired",
            entity_type="confirmation",
            entity_id=confirmation.id,
            entity_ref=confirmation.entity_ref,
            entity_version=confirmation.entity_version,
            before={"status": ConfirmationStatus.PENDING.value},
            after={"status": ConfirmationStatus.RETIRED.value, "run": run.id},
            reason=f"approval by {actor} found the binding stale: {why}",
        )
        stale_run(session, run, why, actor=KERNEL)
        return None
    decide_once(session, confirmation, ConfirmationStatus.APPROVED, actor, reason)
    record_event(
        session,
        actor=actor,
        action="confirmation.approved",
        entity_type="confirmation",
        entity_id=confirmation.id,
        entity_ref=confirmation.entity_ref,
        after={"run": run.id},
        reason=reason,
    )
    _bump_pair(session, packet, run, "parked run approved", actor)
    lease = now + timedelta(minutes=get_int(session, "queue.lease_minutes"))
    _packet_cas(
        session,
        packet,
        S.RUNNING,
        actor,
        reason or "parked run approved",
        where=[Packet.__table__.c.active_run_id == run.id],
        lease_expires_at=lease,
        heartbeat_at=now,
    )
    return authorise_dispatch(
        session,
        run,
        actor=actor,
        now=now,
        demands=demands,
        approval=confirmation,
        _resume_lease=lease,
    )


def reject_run(session: Session, confirmation_id: str, *, actor: Actor, reason: str) -> None:
    """Reject a parked run (E2, D10 Q5): the packet is held with the reason; the run stays
    parked, its G moved with the packet's so the rejection does not stale it."""
    from orchestrator.confirmations import decide_once

    if not actor.is_user:
        raise PermissionError("only a user may reject a parked run")
    if not reason.strip():
        raise RunError("a rejection needs a reason")
    confirmation, run, packet = _parked(session, confirmation_id)
    decide_once(session, confirmation, ConfirmationStatus.REJECTED, actor, reason)
    record_event(
        session,
        actor=actor,
        action="confirmation.rejected",
        entity_type="confirmation",
        entity_id=confirmation.id,
        entity_ref=confirmation.entity_ref,
        after={"run": run.id},
        reason=reason,
    )
    _bump_pair(session, packet, run, "parked run rejected", actor)
    _packet_cas(
        session,
        packet,
        S.HELD,
        actor,
        f"parked run rejected: {reason}",
        where=[
            Packet.__table__.c.active_run_id == run.id,
            Packet.__table__.c.fencing_token == run.fencing_token,
        ],
    )


def cancel_run(session: Session, run_id: str, *, actor: Actor, reason: str) -> None:
    """Explicitly cancel a parked run after its rejection (C27, E2): the run fails without
    using the packet's failure budget; the packet stays ``held``."""
    if not actor.is_user:
        raise PermissionError("only a user may cancel a parked run")
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise RunError(f"no run {run_id}")
    pending = session.scalar(
        select(Confirmation.id).where(
            Confirmation.entity_id == run.id, Confirmation.status == ConfirmationStatus.PENDING
        )
    )
    if pending is not None:
        raise RunError("decide the run's pending confirmation first")
    move_run(
        session,
        run,
        R.FAILED,
        actor,
        f"cancelled: {reason}",
        failure_kind="cancelled",
        charged=False,
    )
    packet = session.get(Packet, run.packet_id)
    if packet is not None and not packet.locked and packet.active_run_id == run.id:
        table = Packet.__table__
        result = session.connection().execute(
            update(table)
            .where(
                table.c.id == packet.id,
                table.c.active_run_id == run.id,
                table.c.fencing_token == run.fencing_token,
            )
            .values(active_run_id=None, recovery_pending=False)
        )
        if result.rowcount != 1:
            raise LostRace(f"{packet.ref}: cancellation lost packet ownership")
        set_committed_value(packet, "active_run_id", None)
        set_committed_value(packet, "recovery_pending", False)
        record_event(
            session,
            actor=actor,
            action="packet.run_released",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            before={"active_run_id": run.id},
            after={"active_run_id": None},
            reason=f"run cancelled: {reason}",
        )
        session.flush()


# --------------------------------------------------------------------------- kernel claim


def try_claim(
    factory: sessionmaker[Session], ref: str, worker: str, *, now: datetime | None = None
) -> str | None:
    """The kernel's claim (M2.1 ruling 5, C25): a lost claim — including SQLite's database lock —
    rolls back and is retried next tick. Returns the run id, or ``None``. (M3a retired the
    context-changed revert of ruling 4: upstream edits raise ``question_context`` instead.)"""
    from sqlalchemy.exc import OperationalError

    from orchestrator.queue import ClaimError, claim

    with factory() as session:
        try:
            run = claim(session, ref, worker, now=now)
            session.commit()
            return run.id
        except ClaimError as exc:
            session.rollback()
            log.info("%s: claim lost (%s)", ref, exc)
            return None
        except OperationalError as exc:
            session.rollback()
            if session.get_bind().dialect.name == "sqlite" and "locked" in str(exc).lower():
                log.info("%s: SQLite lock during claim; retry next tick (ruling 5)", ref)
                return None
            raise


# --------------------------------------------------------------------------- crashes (R6 OQ18)


def crash_cooldown_seconds(session: Session, crash_count: int) -> int:
    """``min(max, base · factor^(crash_count − 1))`` in whole seconds (integer only)."""
    from orchestrator.settings_store import get_int

    base = get_int(session, "crash.cooldown_base_sec")
    cap = get_int(session, "crash.cooldown_max_sec")
    factor = get_int(session, "crash.cooldown_factor")
    return min(cap, base * (factor ** max(0, crash_count - 1)))


def record_crash(
    session: Session,
    run: GenerationRun,
    reason: str,
    *,
    actor: Actor = KERNEL,
    now: datetime | None = None,
    lock: KernelLock | None = None,
) -> int:
    """A generation that ended with **no gradable output** (R6 OQ18; Q4 crash ≠ grade).

    The run becomes ``crashed``; ``crash_count + 1``; the packet goes ``running → queued`` with
    a cooldown ``min(3600, 30 · 2^(n−1))`` s and its ownership and lease released; at
    ``crash.limit`` (5) it is then held (``hold_reason = crash_limit``). A ``crash_events`` row
    feeds route health: 3 crashes on a channel within 300 s make the route unhealthy for 600 s.
    No grade, no seal, no child, no ``gen_eva``. Returns the new crash count."""
    from datetime import timedelta as _td

    from orchestrator.db.models import CrashEvent, RouteHealth
    from orchestrator.lifecycle import transition
    from orchestrator.settings_store import get_int

    _require_fence(lock, session, now)
    now = now or utcnow()
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    session.refresh(packet)
    if packet.active_run_id != run.id or packet.fencing_token != run.fencing_token:
        raise StaleRun(f"run {run.id}: a crash report for a run that no longer owns the packet")
    move_run(session, run, R.CRASHED, actor, f"crash: {reason}", failure_kind="crash")
    count = packet.crash_count + 1
    cooldown = now + _td(seconds=crash_cooldown_seconds(session, count))
    table = Packet.__table__
    _packet_cas(
        session,
        packet,
        S.QUEUED,
        actor,
        f"crash {count}: {reason} (no gradable output; cooldown until {cooldown.isoformat()})",
        where=[table.c.active_run_id == run.id, table.c.fencing_token == run.fencing_token],
        crash_count=count,
        cooldown_until=cooldown,
        active_run_id=None,
        claimed_by=None,
        lease_expires_at=None,
        heartbeat_at=None,
        recovery_pending=False,
    )
    crash = CrashEvent(
        id=new_id(),
        packet_id=packet.id,
        run_id=run.id,
        channel=Channel(run.channel).value if run.channel else None,
        reason=reason,
        created_at=now,
    )
    session.add(crash)
    record_event(
        session,
        actor=actor,
        action="run.crashed",
        entity_type="crash_event",
        entity_id=crash.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"crash_count": count, "run": run.id, "channel": crash.channel},
        reason=reason,
    )
    session.flush()
    if crash.channel is not None:
        window = get_int(session, "route.crash_window_sec")
        recent = session.scalar(
            select(func.count())
            .select_from(CrashEvent)
            .where(
                CrashEvent.channel == crash.channel,
                CrashEvent.created_at > now - _td(seconds=window),
            )
        )
        if (recent or 0) >= get_int(session, "route.crash_threshold"):
            health = session.get(RouteHealth, crash.channel)
            until = now + _td(seconds=get_int(session, "route.unhealthy_sec"))
            if health is None:
                health = RouteHealth(channel=crash.channel, crashes=0)
                session.add(health)
            health.unhealthy_until = until
            health.crashes = int(recent or 0)
            record_event(
                session,
                actor=actor,
                action="route.unhealthy",
                entity_type="route_health",
                entity_id=crash.channel,
                after={"unhealthy_until": until.isoformat(), "crashes_in_window": recent},
                reason=f"{recent} crashes within {window} s (R6 OQ18)",
            )
            session.flush()
    if count >= get_int(session, "crash.limit"):
        record_event(
            session,
            actor=actor,
            action="packet.hold_reason",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            after={"hold_reason": "crash_limit", "crash_count": count},
        )
        packet.hold_reason = "crash_limit"
        transition(packet, S.HELD, actor, f"crash limit reached ({count})", session=session)
    return count


def abort_run(session: Session, run_id: str, *, actor: Actor, reason: str) -> None:
    """CFO_ABORT (R6 OQ19): an owner's hard halt of a running generation revokes its lease; the
    run is stale and the packet returns to ``queued``."""
    if not actor.is_user:
        raise PermissionError("only a user may abort a run")
    if not reason.strip():
        raise RunError("an abort needs a reason")
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise RunError(f"no run {run_id}")
    record_event(
        session,
        actor=actor,
        action="run.abort_requested",
        entity_type="generation_run",
        entity_id=run.id,
        entity_version=run.packet_version,
        reason=reason,
    )
    stale_run(session, run, f"CFO_ABORT by {actor}: {reason}", actor=KERNEL)
