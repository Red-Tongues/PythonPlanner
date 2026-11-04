"""M3 Stage A: fencing (C26), the dispatch guard (E3), outcomes, acceptance and staleness
(C25, E7, E8), parked-run decisions (R1, E2, D10 Q5), pauses (C26, Q4), the kernel lock (C26),
scope-wide gate bumps (C25) and the kernel's claim error handling (M2.1 rulings 4–5).

Single-session behaviour; the two-session races are in ``test_concurrency.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.budgets import BudgetExhausted, Demand, reconcile, reserve
from orchestrator.confirmations import ConfirmationError
from orchestrator.contracts import (
    AttemptOutcome,
    AutonomyMode,
    GenerationRunState,
    OverrideKind,
    PacketStatus,
    Stage,
)
from orchestrator.control import DispatchRefused, FencedOut, KernelLock, LockHeld, pause, resume
from orchestrator.db.models import (
    BudgetCounter,
    ChannelRun,
    ChannelRunOutcome,
    Confirmation,
    Event,
    GenerationRun,
    Packet,
)
from orchestrator.errors import InvalidTransitionError
from orchestrator.gates import bump_scope
from orchestrator.governance import add_override, grant_waiver
from orchestrator.queue import claim
from orchestrator.repository import get_packet, set_autonomy_mode, supersede_objective
from orchestrator.runs import (
    LostRace,
    RunError,
    StaleRun,
    accept_run,
    approve_run,
    assert_publishable,
    authorise_dispatch,
    binding_mismatch,
    cancel_run,
    move_run,
    park_run,
    reconcile_runs,
    record_outcome,
    reject_run,
    try_claim,
)
from tests.conftest import KERNEL, USER
from tests.runfixtures import attempts, claimed_run, held, queued_packet

R, S = GenerationRunState, PacketStatus


def _dispatched(session: Session):  # type: ignore[no-untyped-def]
    packet, run = claimed_run(session)
    attempt = authorise_dispatch(session, run, lock=held(session))
    move_run(session, run, R.AWAITING_RESULT, KERNEL, "launched")
    return packet, run, attempt


# --------------------------------------------------------------------------- claim and fencing


def test_claim_fences_the_packet_and_plans_the_run(session: Session) -> None:
    packet, run = claimed_run(session)
    assert run.state is R.PLANNED
    assert (run.fencing_token, packet.fencing_token) == (1, 1)
    assert packet.active_run_id == run.id and run.gate_revision == packet.gate_revision
    assert run.channel == "local_generation" and run.max_attempts == 3
    assert binding_mismatch(session, run) is None


def test_every_claim_carries_a_higher_token(session: Session) -> None:
    packet, run = claimed_run(session)
    stale_reason = "test: give the packet back"
    from orchestrator.runs import stale_run

    stale_run(session, run, stale_reason)
    assert packet.status is S.EVALUATING_PRE and packet.active_run_id is None
    for status in (S.READY, S.QUEUED):
        from orchestrator.lifecycle import transition

        transition(packet, status, KERNEL)
    second = claim(session, packet.ref, "kernel-b")
    assert second.fencing_token == 2 and packet.fencing_token == 2
    assert "obsolete" in (binding_mismatch(session, run) or "") or run.state is R.STALE


# --------------------------------------------------------------------------- dispatch guard (E3)


def test_dispatch_writes_intent_before_any_effect(session: Session) -> None:
    packet, run = claimed_run(session)
    attempt = authorise_dispatch(session, run, lock=held(session))
    assert run.state is R.DISPATCHED and run.attempts == 1
    assert attempt.operation_id == f"{run.id}:1" and attempt.fencing_token == 1
    assert attempt.run_id.startswith("LG") and attempt.generation_run_id == run.id
    event = session.scalar(select(Event).where(Event.action == "run.attempt_authorised"))
    assert event is not None and event.after["console"] == f"#{attempt.run_id} [attempt 1/3]"
    view = session.execute(
        __import__("sqlalchemy").text(
            "SELECT run_id, attempt, outcome FROM v_run_attempts WHERE generation_run_id = :r"
        ),
        {"r": run.id},
    ).one()
    assert tuple(view) == (attempt.run_id, 1, None)


def test_retries_pass_the_same_guard_and_stop_at_max_attempts(session: Session) -> None:
    packet, run, first = _dispatched(session)
    record_outcome(session, first, AttemptOutcome.FAILED, exit_code=1)
    second = authorise_dispatch(
        session, run, lock=held(session)
    )  # awaiting_result → awaiting_result (retry)
    third = authorise_dispatch(session, run, lock=held(session))
    assert [a.attempt for a in (first, second, third)] == [1, 2, 3]
    assert run.state is R.AWAITING_RESULT and attempts(session) == 3
    with pytest.raises(RunError, match="used all 3 attempts"):
        authorise_dispatch(session, run, lock=held(session))


def test_a_governance_write_stales_the_run_before_dispatch(session: Session) -> None:
    packet, run = claimed_run(session)
    add_override(session, packet.ref, OverrideKind.HOLD, actor=USER, reason="wait")
    with pytest.raises(StaleRun, match="gate revision"):
        authorise_dispatch(session, run, lock=held(session))
    assert attempts(session) == 0 and run.state is R.PLANNED  # unchanged on refusal
    assert reconcile_runs(session) == [run.id]
    # E7/V2: a HOLD applies, so only the run goes stale; the packet keeps its state
    assert run.state is R.STALE and packet.status is S.RUNNING and packet.active_run_id is None


def test_stale_reset_returns_an_unrestricted_owned_packet_to_evaluation(session: Session) -> None:
    packet, run = claimed_run(session)
    grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="fine")  # G + 1
    assert reconcile_runs(session) == [run.id]
    assert run.state is R.STALE and packet.status is S.EVALUATING_PRE


def test_a_stale_run_never_moves_a_packet_it_does_not_own(session: Session) -> None:
    packet, run = claimed_run(session)
    table = Packet.__table__
    session.connection().execute(
        table.update().where(table.c.id == packet.id).values(active_run_id="someone-else")
    )
    session.refresh(packet)
    from orchestrator.runs import stale_run

    stale_run(session, run, "test")
    assert run.state is R.STALE and packet.status is S.RUNNING
    assert packet.active_run_id == "someone-else"


def test_pause_refuses_dispatch_without_touching_gates(session: Session) -> None:
    packet, run = claimed_run(session)
    gate = packet.gate_revision
    objective_ref = packet.objective.ref
    assert pause(session, "objective", objective_ref, actor=USER, reason="look first")
    assert packet.gate_revision == gate  # D10 Q4: a pause is a control, not a gate write
    with pytest.raises(DispatchRefused, match=f"objective:{objective_ref} is paused"):
        authorise_dispatch(session, run, lock=held(session))
    assert attempts(session) == 0
    with pytest.raises(PermissionError):
        resume(session, "objective", objective_ref, actor=KERNEL, reason="auto")  # never automatic
    assert resume(session, "objective", objective_ref, actor=USER, reason="go")
    assert authorise_dispatch(session, run, lock=held(session)).attempt == 1
    for kind in ("scope.paused", "scope.resumed"):
        assert session.scalar(select(func.count()).select_from(Event).where(Event.action == kind))


def test_a_stale_run_keeps_a_paused_packet_where_it_is(session: Session) -> None:
    packet, run = claimed_run(session)
    pause(session, "global", "*", actor=USER, reason="stop everything")
    grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="x")
    reconcile_runs(session)
    assert run.state is R.STALE and packet.status is S.RUNNING  # restriction kept (V2)


def test_dispatch_reserves_budget_or_changes_nothing(session: Session) -> None:
    packet, run = claimed_run(session)
    hour = datetime(2026, 11, 4, 12, tzinfo=UTC)
    demand = Demand("channel", "local_generation", "runs", "hour", hour, 1, 1)
    authorise_dispatch(session, run, demands=[demand], lock=held(session))
    used = session.scalar(select(BudgetCounter.used))
    assert used == 1
    record_outcome(session, session.scalars(select(ChannelRun)).one(), AttemptOutcome.FAILED)
    move_run(session, run, R.AWAITING_RESULT, KERNEL, "launched")
    with pytest.raises(BudgetExhausted):
        authorise_dispatch(session, run, demands=[demand], lock=held(session))
    session.rollback()


def test_reservations_reconcile_exactly_once(session: Session) -> None:
    hour = datetime(2026, 11, 4, 12, tzinfo=UTC)
    demand = Demand("objective", "O-1", "tokens", "day", hour, 500, 600)
    (rid,) = reserve(session, "op-1", [demand], actor=KERNEL)
    with pytest.raises(BudgetExhausted):
        reserve(
            session,
            "op-2",
            [Demand("objective", "O-1", "tokens", "day", hour, 200, 600)],
            actor=KERNEL,
        )
    session.rollback()
    (rid,) = reserve(session, "op-1", [demand], actor=KERNEL)
    assert reconcile(session, rid, 120, actor=KERNEL)
    assert not reconcile(session, rid, 120, actor=KERNEL)  # a duplicate report changes nothing
    assert session.scalar(select(BudgetCounter.used)) == 120


# --------------------------------------------------------------------------- outcomes, acceptance


def test_outcomes_are_recorded_once(session: Session) -> None:
    _, _, attempt = _dispatched(session)
    assert record_outcome(session, attempt, AttemptOutcome.SUCCEEDED, exit_code=0) is not None
    assert record_outcome(session, attempt, AttemptOutcome.SUCCEEDED, exit_code=0) is None
    assert session.scalar(select(func.count()).select_from(ChannelRunOutcome)) == 1


def test_acceptance_needs_every_binding(session: Session) -> None:
    packet, run, attempt = _dispatched(session)
    record_outcome(session, attempt, AttemptOutcome.SUCCEEDED, exit_code=0)
    move_run(session, run, R.VALIDATING, KERNEL, "exit 0")
    grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="x")
    with pytest.raises(StaleRun):
        accept_run(session, run, "m" * 64, lock=held(session))
    assert run.state is R.STALE


def test_accepted_run_goes_stale_before_publication_on_a_context_change(session: Session) -> None:
    packet, run, _ = _dispatched(session)
    move_run(session, run, R.VALIDATING, KERNEL, "exit 0")
    accept_run(session, run, "m" * 64, lock=held(session))
    lock = held(session)
    assert_publishable(session, run, lock=lock)  # all bindings hold
    supersede_objective(session, packet.objective.ref, {"brief": "changed"}, actor=USER)
    with pytest.raises(StaleRun):  # E8: H mismatch (and G bump) before the commit is recorded
        assert_publishable(session, run, lock=lock)
    assert run.state is R.STALE


def test_a_late_result_for_an_obsolete_token_revives_nothing(session: Session) -> None:
    packet, run, attempt = _dispatched(session)
    later = datetime.now(UTC) + timedelta(hours=2)  # the lease (30 min) has expired
    assert reconcile_runs(session, now=later) == [run.id]
    assert run.state is R.STALE
    from orchestrator.lifecycle import transition

    for status in (S.READY, S.QUEUED):
        transition(packet, status, KERNEL)
    claim(session, packet.ref, "kernel-b", now=later)  # takeover: F = 2
    late = record_outcome(session, attempt, AttemptOutcome.SUCCEEDED, exit_code=0)
    assert late is not None and late.outcome == AttemptOutcome.STALE
    with pytest.raises(InvalidTransitionError):
        move_run(session, run, R.VALIDATING, KERNEL, "late")  # stale is final


# --------------------------------------------------------------------------- parked runs


def _parked(session: Session):  # type: ignore[no-untyped-def]
    packet, run, attempt = _dispatched(session)
    record_outcome(session, attempt, AttemptOutcome.PARKED, exit_code=3)
    confirmation = park_run(session, run, lock=held(session))
    return packet, run, confirmation


def test_parking_keeps_the_run_matched_and_resumes_once_on_approval(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    assert run.state is R.AWAITING_CONFIRMATION and packet.status is S.AWAITING_CONFIRMATION
    assert run.gate_revision == packet.gate_revision  # creation bumped both
    before = packet.gate_revision
    resumed = approve_run(session, confirmation.id, actor=USER)
    assert run.state is R.DISPATCHED and packet.status is S.RUNNING
    assert run.gate_revision == packet.gate_revision == before + 1  # R1
    assert resumed.attempt == 2 and attempts(session) == 2
    with pytest.raises(ConfirmationError):  # decided once
        approve_run(session, confirmation.id, actor=USER)


def test_approval_after_another_governance_write_is_refused(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="x")  # not the approval
    with pytest.raises((StaleRun, LostRace)):
        approve_run(session, confirmation.id, actor=USER)
    assert attempts(session) == 1  # nothing new was authorised (the caller rolls back)


def test_rejection_holds_the_packet_and_leaves_the_run_parked(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    reject_run(session, confirmation.id, actor=USER, reason="not like this")
    assert packet.status is S.HELD and run.state is R.AWAITING_CONFIRMATION
    assert run.gate_revision == packet.gate_revision  # Q5: bumped together
    assert reconcile_runs(session) == []  # E2: the rejection does not stale its own run
    cancel_run(session, run.id, actor=USER, reason="drop it")
    assert run.state is R.FAILED and not run.charged and run.failure_kind == "cancelled"
    assert packet.status is S.HELD and packet.active_run_id is None


def test_cancel_needs_the_decision_first_and_an_owner(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    with pytest.raises(RunError, match="pending confirmation"):
        cancel_run(session, run.id, actor=USER, reason="x")
    reject_run(session, confirmation.id, actor=USER, reason="no")
    with pytest.raises(PermissionError):
        cancel_run(session, run.id, actor=KERNEL, reason="x")


def test_a_later_unrelated_change_stales_a_rejected_parked_run(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    reject_run(session, confirmation.id, actor=USER, reason="no")
    add_override(session, packet.ref, OverrideKind.BLOCK, actor=USER, reason="kill branch")
    assert reconcile_runs(session) == [run.id]
    assert run.state is R.STALE and packet.status is S.HELD


# --------------------------------------------------------------------------- kernel lock


def test_kernel_lock_exclusion_expiry_and_takeover(session: Session) -> None:
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    a, b = KernelLock("kernel-a"), KernelLock("kernel-b")
    assert a.acquire(session, now=now) == 1
    with pytest.raises(LockHeld):
        b.acquire(session, now=now + timedelta(seconds=30))
    a.heartbeat(session, now=now + timedelta(seconds=60))  # extends to 3 min
    with pytest.raises(LockHeld):
        b.acquire(session, now=now + timedelta(minutes=2))
    assert b.acquire(session, now=now + timedelta(minutes=4)) == 2  # expired → takeover
    with pytest.raises(FencedOut):  # the old owner can no longer write anything guarded
        a.fence(session, now=now + timedelta(minutes=4))
    actions = session.scalars(select(Event.action).where(Event.entity_type == "kernel_lease"))
    assert list(actions) == ["kernel_lock.acquired", "kernel_lock.takeover"]
    b.release(session, now=now + timedelta(minutes=5))
    assert KernelLock("kernel-c").acquire(session, now=now + timedelta(minutes=5)) == 3


def test_a_fenced_out_kernel_cannot_dispatch_or_publish(session: Session) -> None:
    packet, run = claimed_run(session)
    now = datetime.now(UTC)
    old, new = KernelLock("kernel-a"), KernelLock("kernel-b")
    old.acquire(session, now=now)
    new.acquire(session, now=now + timedelta(minutes=10))
    with pytest.raises(FencedOut):
        authorise_dispatch(session, run, lock=old, now=now + timedelta(minutes=10))
    assert attempts(session) == 0
    authorise_dispatch(session, run, lock=new, now=now + timedelta(minutes=10))
    move_run(session, run, R.AWAITING_RESULT, KERNEL, "x")
    move_run(session, run, R.VALIDATING, KERNEL, "x")
    accept_run(session, run, "m" * 64, lock=new, now=now + timedelta(minutes=10))
    with pytest.raises(FencedOut):
        assert_publishable(session, run, lock=old, now=now + timedelta(minutes=10))
    assert_publishable(session, run, lock=new, now=now + timedelta(minutes=10))


# --------------------------------------------------------------------------- C25 scope bumps


def test_scope_bumps_reach_every_open_packet_in_scope(session: Session) -> None:
    packet = queued_packet(session)
    others = [p for p in session.scalars(select(Packet)) if p.id != packet.id]
    before = {p.id: p.gate_revision for p in session.scalars(select(Packet))}
    set_autonomy_mode(session, Stage.BRANCHING, AutonomyMode.AUTO, actor=USER)  # global
    for p in session.scalars(select(Packet)):
        assert p.gate_revision == before[p.id] + 1
    supersede_objective(session, packet.objective.ref, {"brief": "new"}, actor=USER)
    for p in [packet, *others]:
        session.refresh(p)
        assert p.gate_revision == before[p.id] + 2  # same objective
    ids = bump_scope(session, "packet", packet.ref, actor=USER, cause="test")
    assert ids == [packet.id]
    event = session.scalars(
        select(Event)
        .where(Event.action == "packet.gate_revision")
        .order_by(Event.created_at.desc())
    ).first()
    assert event is not None and event.after["scope"] == ["packet", packet.ref]


def test_terminal_packets_are_never_bumped(session: Session) -> None:
    queued_packet(session)
    from orchestrator.lifecycle import transition

    other = get_packet(session, "PP-000002")
    transition(other, S.CLOSED_UNVERIFIED, USER, "not needed")
    gate = other.gate_revision
    bump_scope(session, "global", "*", actor=USER, cause="test")
    session.refresh(other)
    assert other.gate_revision == gate


def test_ingestion_confirmation_decision_bumps_the_project(session: Session) -> None:
    from orchestrator.confirmations import reject

    packet = queued_packet(session)
    ingestion = session.scalar(select(Confirmation).where(Confirmation.entity_type == "project"))
    assert ingestion is not None
    gate = packet.gate_revision
    reject(session, ingestion.id, actor=USER, reason="re-ingest")
    session.refresh(packet)
    assert packet.gate_revision == gate + 1


# --------------------------------------------------------------------------- rulings 4–5


def test_try_claim_commits_a_context_change(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        packet = queued_packet(s)
        ref, objective = packet.ref, packet.objective.ref
        s.commit()
    with factory() as s:  # the objective changes behind the queue (no gate bump on purpose)
        table = Packet.__table__
        s.connection().execute(table.update().where(table.c.ref == ref).values(input_hash="x"))
        s.commit()
    assert try_claim(factory, ref, "kernel-a") is None
    with factory() as s:
        assert get_packet(s, ref).status is S.EVALUATING_PRE  # persisted, not rolled back
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0
    assert objective


def test_try_claim_returns_the_run(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ref = queued_packet(s).ref
        s.commit()
    run_id = try_claim(factory, ref, "kernel-a")
    assert run_id is not None
    assert try_claim(factory, ref, "kernel-b") is None  # lost: already running


def test_scope_bumps_skip_superseded_history(session: Session) -> None:
    from orchestrator.repository import supersede_packet

    packet = queued_packet(session)
    old_id = packet.id
    supersede_packet(session, packet.ref, {"title": "v2"}, actor=USER)
    ids = bump_scope(session, "global", "*", actor=USER, cause="test")
    assert old_id not in ids and get_packet(session, packet.ref).id in ids


def test_an_unrouted_run_is_never_dispatched(session: Session) -> None:
    from orchestrator.repository import update_packet

    packet = queued_packet(session)
    update_packet(session, packet, {"channel": None}, actor=USER, reason="unroute")
    run = claim(session, packet.ref, "kernel-a")
    assert run.channel is None
    with pytest.raises(RunError, match="no channel"):
        authorise_dispatch(session, run, lock=held(session))
    assert attempts(session) == 0


# ------------------------------------------------------------------- authority (R4 MR2, MR3)


def test_kernel_writes_without_the_lock_are_refused(session: Session) -> None:
    from orchestrator.control import FencedOut

    packet, run = claimed_run(session)
    with pytest.raises(FencedOut):
        authorise_dispatch(session, run)
    attempt = authorise_dispatch(session, run, lock=held(session))
    move_run(session, run, R.AWAITING_RESULT, KERNEL, "launched")
    record_outcome(session, attempt, AttemptOutcome.PARKED, exit_code=3)
    with pytest.raises(FencedOut):
        park_run(session, run)
    move_run(session, run, R.VALIDATING, KERNEL, "exit 0")
    with pytest.raises(FencedOut):
        accept_run(session, run, "m" * 64)
    assert run.state is R.VALIDATING and attempts(session) == 1


def test_only_an_approval_resumes_a_parked_run(session: Session) -> None:
    packet, run, confirmation = _parked(session)
    with pytest.raises(RunError, match="only through approve_run"):
        authorise_dispatch(session, run, lock=held(session))  # the kernel cannot resume it
    with pytest.raises(RunError, match="via approve_run"):
        authorise_dispatch(session, run, actor=USER)  # nor a user without the approval
    with pytest.raises(RunError, match="not a decided approval"):
        authorise_dispatch(session, run, actor=USER, approval=confirmation)  # still pending
    reject_run(session, confirmation.id, actor=USER, reason="no")
    with pytest.raises(RunError):  # MR3: a rejected parked run is never resumable directly
        authorise_dispatch(session, run, actor=USER, approval=confirmation)
    assert run.state is R.AWAITING_CONFIRMATION and attempts(session) == 1
