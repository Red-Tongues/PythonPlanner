"""Durable generation runs (M3 C24–C27, D8 with errata E2/E3/E7/E8, D10 Q5).

Every run state change goes through ``move_run``: the M3 run table (``orchestrator.transitions``),
then one compare-and-set UPDATE on (id, state, fencing token), then an event. The database
triggers of migration 0007 enforce the same table and the frozen bindings.

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
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
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
from orchestrator.control import KernelLock, authorise_scopes, scope_status
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
        after={"status": target.value, **{k: v for k, v in values.items() if k != "updated_at"}},
        reason=reason,
    )
    session.flush()


# --------------------------------------------------------------------------- bindings


def binding_mismatch(
    session: Session, run: GenerationRun, *, now: datetime | None = None, lease: bool = True
) -> str | None:
    """Why ``run`` may no longer act, or ``None`` when every binding holds (C25, C26, E7, E8)."""
    from orchestrator.gates import current_fingerprint

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
    if packet.gate_revision != run.gate_revision:
        return f"gate revision changed ({run.gate_revision} → {packet.gate_revision})"
    if (
        packet.input_hash != run.input_hash
        or current_fingerprint(session, packet) != run.input_hash
    ):
        return "context fingerprint changed"
    if lease and run.lease_expires_at is not None and run.lease_expires_at < (now or utcnow()):
        return "lease expired"
    return None


def _restricted(session: Session, packet: Packet) -> str | None:
    """A hold, block, pause or confirmation that keeps a packet where it is (C25 / V2 / E7)."""
    from orchestrator.governance import active_overrides

    for o in active_overrides(session, packet):
        if OverrideKind(o.kind) in (OverrideKind.BLOCK, OverrideKind.HOLD):
            return f"{OverrideKind(o.kind).value} override"
    for scope in packet_scopes(session, packet):
        if scope_status(session, *scope) is ScopeStatus.PAUSED:
            return f"scope {scope[0]}:{scope[1]} paused"
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
    if packet.root_id:
        root = session.get(Packet, packet.root_id)
        if root is not None:
            scopes.append(("branch", root.ref))
    if packet.channel:
        scopes.append(("channel", Channel(packet.channel).value))
    return scopes


# --------------------------------------------------------------------------- staleness


def stale_run(session: Session, run: GenerationRun, reason: str, *, actor: Actor = KERNEL) -> None:
    """Make ``run`` stale (evented). The packet returns to ``evaluating_pre`` only when it is
    still ``running`` and owned by this run with this run's F, and nothing restricts it (E7);
    otherwise its state and restrictions are kept. Terminal packets are never touched."""
    move_run(session, run, R.STALE, actor, reason, stale_reason=reason)
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
    if (
        PacketStatus(packet.status) is S.RUNNING
        and is_head
        and _restricted(session, packet) is None
    ):
        _packet_cas(
            session,
            packet,
            S.EVALUATING_PRE,
            actor,
            f"run {run.id} stale: {reason}",
            where=owner,
            active_run_id=None,
            claimed_by=None,
            lease_expires_at=None,
        )
    else:  # keep state and restrictions; only release the ownership (evented)
        session.connection().execute(
            update(table).where(table.c.id == packet.id, *owner).values(active_run_id=None)
        )
        set_committed_value(packet, "active_run_id", None)
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


def reconcile_runs(session: Session, *, now: datetime | None = None) -> list[str]:
    """Stale every active run whose bindings no longer hold (startup and every tick). A parked
    run has no lease to expire: it waits for its owner, but any other governance change still
    stales it (E2)."""
    staled: list[str] = []
    for run in session.scalars(
        select(GenerationRun).where(GenerationRun.state.in_([s.value for s in RUN_ACTIVE]))
    ).all():
        if run.state == R.ACCEPTED and run.commit_sha is not None:
            continue
        parked = GenerationRunState(run.state) is R.AWAITING_CONFIRMATION
        why = binding_mismatch(session, run, now=now, lease=not parked)
        if why is not None:
            stale_run(session, run, why)
            staled.append(run.id)
    return staled


# --------------------------------------------------------------------------- dispatch (E3)

_DISPATCH_FROM = {
    R.PLANNED: R.DISPATCHED,
    R.AWAITING_CONFIRMATION: R.DISPATCHED,  # exit-3 resume, after R1
    R.AWAITING_RESULT: R.AWAITING_RESULT,  # exit 1/4 retry
    R.VALIDATING: R.AWAITING_RESULT,  # validation retry
    R.DISPATCHED: R.DISPATCHED,  # deadline retry (E11)
}


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
) -> ChannelRun:
    """The single dispatch guard (E3). Returns the new immutable attempt row; the caller commits
    and only then performs the effect under ``attempt.operation_id``.

    Raises ``StaleRun`` (bindings), ``control.DispatchRefused`` (paused), ``FencedOut``
    (kernel lock) or ``budgets.BudgetExhausted``; on any of these the caller rolls back and the
    packet and run are unchanged. ``before_scopes`` is a test seam.
    """
    now = now or utcnow()
    source = GenerationRunState(run.state)
    if source not in _DISPATCH_FROM:
        raise RunError(f"run {run.id} cannot be dispatched from {source.value}")
    if run.channel is None:  # fail closed: never guess a channel for an unrouted packet
        raise RunError(f"run {run.id} has no channel (the packet was not routed)")
    if lock is not None:
        lock.fence(session, now=now)
    parked = source is R.AWAITING_CONFIRMATION
    why = binding_mismatch(session, run, now=now, lease=not parked)
    if why is not None:
        raise StaleRun(f"run {run.id}: {why}")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    if before_scopes is not None:
        before_scopes()
    authorise_scopes(session, packet_scopes(session, packet))
    attempt_no = run.attempts + 1
    if run.max_attempts is not None and attempt_no > run.max_attempts and not parked:
        raise RunError(f"run {run.id} has used all {run.max_attempts} attempts")
    operation_id = f"{run.id}:{attempt_no}"
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
        attempt=attempt_no,
        max_attempts=run.max_attempts or attempt_no,
        contract=contract,
        state="authorised",
        idempotency_key=operation_id,
        input_hash=run.input_hash,
        policy_revision=str(run.gate_revision),
        status="running",
        started_at=now,
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
            "console": f"#{attempt.run_id} [attempt {attempt_no}/{attempt.max_attempts}]",
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
        f"attempt {attempt_no} authorised ({operation_id})",
        attempts=attempt_no,
        heartbeat_at=now,
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
    binding holds; otherwise the run is made stale and ``StaleRun`` is raised."""
    if lock is not None:
        lock.fence(session, now=now)
    why = binding_mismatch(session, run, now=now)
    if why is not None:
        stale_run(session, run, why, actor=actor)
        raise StaleRun(f"run {run.id}: {why}")
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


# --------------------------------------------------------------------------- parked runs (exit 3)


def _bump_pair(session: Session, packet: Packet, run: GenerationRun, cause: str, actor: Actor):
    """R1 / Q5: check ``run.G == packet.G`` and move both to G + 1 in one transaction, so a
    decision aimed at this run does not stale it. Any concurrent governance write loses here."""
    table, runs = Packet.__table__, GenerationRun.__table__
    observed = run.gate_revision
    if observed is None or packet.gate_revision != observed:
        raise StaleRun(f"run {run.id}: gate revision changed")
    a = session.connection().execute(
        update(table)
        .where(table.c.id == packet.id, table.c.gate_revision == observed)
        .values(gate_revision=observed + 1)
    )
    b = session.connection().execute(
        update(runs)
        .where(runs.c.id == run.id, runs.c.gate_revision == observed)
        .values(gate_revision=observed + 1)
    )
    if a.rowcount != 1 or b.rowcount != 1:
        raise LostRace(f"run {run.id}: a governance write changed the gate first")
    set_committed_value(packet, "gate_revision", observed + 1)
    set_committed_value(run, "gate_revision", observed + 1)
    record_event(
        session,
        actor=actor,
        action="packet.gate_revision",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={"gate_revision": observed + 1, "run": run.id, "cause": cause},
        reason="decision aimed at this run (R1): packet and run bumped together",
    )
    session.flush()


def park_run(session: Session, run: GenerationRun, *, actor: Actor = KERNEL) -> Confirmation:
    """Exit 3: ``awaiting_result → awaiting_confirmation`` (no counters), the packet
    ``running → awaiting_confirmation`` and exactly one confirmation aimed at this run."""
    why = binding_mismatch(session, run)
    if why is not None:
        stale_run(session, run, why, actor=actor)
        raise StaleRun(f"run {run.id}: {why}")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
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
) -> ChannelRun:
    """Approve a parked run (R1, E3): decide the confirmation once, check ``run.G == packet.G``
    and bump both, revalidate every binding, then resume the *same* run through the dispatch
    guard. Any failure raises and the caller rolls back: nothing is decided or dispatched."""
    from orchestrator.confirmations import decide_once

    if not actor.is_user:
        raise PermissionError("only a user may approve a parked run")
    confirmation, run, packet = _parked(session, confirmation_id)
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
    if GenerationRunState(run.state) is not R.AWAITING_CONFIRMATION:
        raise RunError(f"run {run.id} is {GenerationRunState(run.state).value}, not parked")
    why = binding_mismatch(session, run, now=now, lease=False)
    if why is not None:
        raise StaleRun(f"run {run.id}: {why}")
    _bump_pair(session, packet, run, "parked run approved", actor)
    _packet_cas(
        session,
        packet,
        S.RUNNING,
        actor,
        reason or "parked run approved",
        where=[Packet.__table__.c.active_run_id == run.id],
    )
    return authorise_dispatch(session, run, actor=actor, now=now, demands=demands)


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
    _packet_cas(session, packet, S.HELD, actor, f"parked run rejected: {reason}")


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
        session.connection().execute(
            update(table).where(table.c.id == packet.id).values(active_run_id=None)
        )
        set_committed_value(packet, "active_run_id", None)
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
    """The kernel's claim with M2.1 rulings 4–5 (C25): a context mismatch is **committed** (so
    the packet's return to ``evaluating_pre`` persists); a lost claim — including SQLite's
    database lock — rolls back and is retried next tick. Returns the run id, or ``None``."""
    from sqlalchemy.exc import OperationalError

    from orchestrator.queue import ClaimError, ContextChanged, claim

    with factory() as session:
        try:
            run = claim(session, ref, worker, now=now)
            session.commit()
            return run.id
        except ContextChanged:
            session.commit()  # ruling 4: never roll back the audited revert
            log.info("%s: context changed at claim; sent back to evaluating_pre", ref)
            return None
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
