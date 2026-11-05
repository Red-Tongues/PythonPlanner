"""Two-session races: admission and claim (M2.1 fixes 2, 11) and the C38 scenarios for
governance atomicity (C25), fencing, pause and the kernel lock (C26) and reservation (C36).
Each runs on SQLite and, with ORCH_TEST_POSTGRES_URL, on
PostgreSQL (CI's postgres job), with two real sessions on two connections.

Every test asserts row counts, final states and the expected number of *effects*: an effect is
a committed, authorised attempt row (``channel_runs`` with a generation run), which is the only
thing an adapter may act on. An authorised operation produces exactly one; one refused by
governance, pause, fencing or budget produces zero; nothing ever produces two.

On SQLite the losing side can surface as "database is locked" instead of a domain error (one
writer, WAL; M2.1 ruling 5): it is still a loss with no effect.

Out of Stage A: "crash after git commit, before the DB acknowledgement" needs the repository
writer (Stage B).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.budgets import BudgetExhausted, Demand, reserve
from orchestrator.confirmations import ConfirmationError
from orchestrator.contracts import (
    AttemptOutcome,
    GenerationRunState,
    OverrideKind,
    PacketStatus,
    ScopeStatus,
)
from orchestrator.control import DispatchRefused, KernelLock, LockHeld, pause, scope_status
from orchestrator.db.models import (
    BudgetReservation,
    ChannelRun,
    ChannelRunOutcome,
    Confirmation,
    GenerationRun,
    Packet,
    QueueAdmission,
    new_id,
)
from orchestrator.events import record_event
from orchestrator.gates import note_confirmation
from orchestrator.governance import add_override, grant_waiver, revoke
from orchestrator.lifecycle import transition
from orchestrator.queue import ClaimError, admit_next, claim
from orchestrator.repository import get_packet, supersede_objective
from orchestrator.runs import (
    LostRace,
    StaleRun,
    accept_run,
    approve_run,
    authorise_dispatch,
    move_run,
    park_run,
    record_outcome,
    reject_run,
)
from tests.conftest import KERNEL, USER
from tests.govfixtures import (
    claim_race,
    queued_ref,
    ready_packet,
)
from tests.runfixtures import attempts, claimed_run, held, queued_packet

R, S = GenerationRunState, PacketStatus


def _both(
    *workers: Callable[[], str],
    allowed_exceptions: tuple[type[BaseException], ...] = (),
) -> list[str]:
    """Run workers on threads, released together by a barrier; return their outcomes."""
    barrier = threading.Barrier(len(workers))
    results: list[str] = [""] * len(workers)
    failures: list[BaseException | None] = [None] * len(workers)

    def wrap(i: int, fn: Callable[[], str]) -> None:
        try:
            barrier.wait(10)
            results[i] = fn()
        except Exception as exc:  # the loser of a race
            if isinstance(exc, allowed_exceptions):
                results[i] = type(exc).__name__
            else:
                failures[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a race worker did not finish in time"
    failure = next((error for error in failures if error is not None), None)
    if failure is not None:
        raise failure
    return results


def _run_threads(*targets: Callable[[], None], timeout: float = 60) -> None:
    """Start the workers, join them, and fail (never hang or pass silently) on a timeout."""
    failures: list[BaseException] = []

    def run(target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a worker did not finish in time"
    if failures:
        raise failures[0]


def _effects(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return attempts(s)


# --------------------------------------------------------------------------- claims


def test_concurrent_claims_of_one_packet(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ref = queued_packet(s).ref
        s.commit()

    def worker(name: str) -> Callable[[], str]:
        def run() -> str:
            with factory() as s:
                claim(s, ref, name)
                s.commit()
                return "won"

        return run

    outcomes = _both(
        worker("kernel-a"),
        worker("kernel-b"),
        allowed_exceptions=(ClaimError, OperationalError),
    )
    assert outcomes.count("won") == 1, outcomes
    assert all(o == "won" or o in {"ClaimError", "OperationalError"} for o in outcomes), outcomes
    with factory() as s:
        packet = get_packet(s, ref)
        runs = s.scalars(select(GenerationRun)).all()
        assert len(runs) == 1 and packet.status is S.RUNNING
        assert packet.fencing_token == runs[0].fencing_token == 1
        assert packet.active_run_id == runs[0].id


def _claim_with_writer(factory: sessionmaker[Session], ref: str, writer) -> list[str]:  # type: ignore[no-untyped-def]
    """Session A checks readiness and builds the snapshot; session B commits ``writer``; then
    A's guarded UPDATE runs."""
    outcome: list[str] = []
    failures: list[BaseException] = []
    checked, written = threading.Event(), threading.Event()

    def worker_a() -> None:
        with factory() as a:

            def seam() -> None:
                checked.set()
                assert written.wait(15), "the concurrent writer did not finish"

            try:
                claim(a, ref, "kernel-a", before_cas=seam)
                a.commit()
                outcome.append("won")
            except (ClaimError, OperationalError) as exc:
                a.rollback()
                outcome.append(type(exc).__name__)

    def worker_b() -> None:
        assert checked.wait(15), "the dispatch check did not reach its barrier"
        try:
            with factory() as b:
                writer(b)
                b.commit()
        except OperationalError:
            pass
        finally:
            written.set()

    def guarded(target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=guarded, args=(worker_a,)),
        threading.Thread(target=guarded, args=(worker_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a claim-race worker did not finish in time"
    if failures:
        raise failures[0]
    return outcome


def test_an_approval_change_between_readiness_and_claim_wins(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        ref = queued_packet(s).ref
        s.commit()

    def new_confirmation(b: Session) -> None:
        packet = get_packet(b, ref)
        confirmation = Confirmation(
            id=new_id(),
            stage="routing",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            summary="route this one by hand",
        )
        b.add(confirmation)
        record_event(
            b,
            actor=USER,
            action="confirmation.created",
            entity_type="confirmation",
            entity_id=confirmation.id,
        )
        b.flush()
        note_confirmation(b, confirmation, actor=USER)

    outcome = _claim_with_writer(factory, ref, new_confirmation)
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0
        assert get_packet(s, ref).fencing_token == 0


def test_a_context_head_edit_between_readiness_and_claim_wins(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        packet = queued_packet(s)
        ref, objective = packet.ref, packet.objective.ref
        s.commit()
    outcome = _claim_with_writer(
        factory,
        ref,
        lambda b: supersede_objective(b, objective, {"brief": "changed meanwhile"}, actor=USER),
    )
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0


# --------------------------------------------------------------------------- budget


def test_two_claimers_race_for_the_last_budget_unit(factory: sessionmaker[Session]) -> None:
    hour = datetime(2026, 11, 4, 12, tzinfo=UTC)
    with factory() as s:  # one unit left of four
        reserve(
            s,
            "op-0",
            [Demand("channel", "local_generation", "runs", "hour", hour, 3, 4)],
            actor=KERNEL,
        )
        s.commit()

    def worker(op: str) -> Callable[[], str]:
        def run() -> str:
            with factory() as s:
                demand = Demand("channel", "local_generation", "runs", "hour", hour, 1, 4)
                reserve(s, op, [demand], actor=KERNEL)
                s.commit()
                return "won"

        return run

    outcomes = _both(
        worker("op-a"),
        worker("op-b"),
        allowed_exceptions=(BudgetExhausted, OperationalError),
    )
    assert outcomes.count("won") == 1, outcomes
    assert all(o == "won" or o in {"BudgetExhausted", "OperationalError"} for o in outcomes)
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(BudgetReservation)) == 2


def test_dispatch_budget_and_acceptance_guards_prevent_unapproved_effects(
    factory: sessionmaker[Session],
) -> None:
    class EffectCountingFakeAdapter:
        def __init__(self) -> None:
            self.dispatches: list[str] = []
            self.publications: list[str] = []

        def dispatch(self, attempt: ChannelRun) -> None:
            self.dispatches.append(attempt.id)

        def publish(self, run: GenerationRun) -> None:
            self.publications.append(run.id)

    adapter = EffectCountingFakeAdapter()
    hour = datetime(2026, 11, 4, 12, tzinfo=UTC)
    demand = Demand("channel", "local_generation", "runs", "hour", hour, 1, 1)
    with factory() as s:
        packet_a, run_a = claimed_run(s)
        packet_b, run_b = claimed_run(s)
        ref_a, run_id_a = packet_a.ref, run_a.id
        ref_b, run_id_b = packet_b.ref, run_b.id
        s.commit()

    with factory() as s:
        run = s.get(GenerationRun, run_id_a)
        assert run is not None
        attempt = authorise_dispatch(s, run, demands=[demand], lock=held(s))
        s.commit()
        adapter.dispatch(attempt)

    with factory() as s:
        run = s.get(GenerationRun, run_id_b)
        assert run is not None
        try:
            authorise_dispatch(s, run, demands=[demand], lock=held(s))
        except BudgetExhausted:
            s.rollback()
        else:
            raise AssertionError("an exhausted budget must refuse dispatch")

    with factory() as s:
        run = s.get(GenerationRun, run_id_a)
        attempt = s.scalar(select(ChannelRun).where(ChannelRun.generation_run_id == run_id_a))
        assert run is not None and attempt is not None
        record_outcome(s, attempt, AttemptOutcome.SUCCEEDED, exit_code=0)
        move_run(s, run, R.AWAITING_RESULT, KERNEL, "adapter completed")
        move_run(s, run, R.VALIDATING, KERNEL, "validate output")
        s.commit()

    with factory() as s:
        packet = get_packet(s, ref_a)
        grant_waiver(s, packet.ref, "tools_needed", actor=USER, reason="gate changed")
        s.commit()

    with factory() as s:
        run = s.get(GenerationRun, run_id_a)
        assert run is not None
        try:
            accept_run(s, run, "m" * 64, lock=held(s))
        except StaleRun:
            s.commit()
        else:
            adapter.publish(run)
            s.commit()
            raise AssertionError("a stale run must not be published")

    assert adapter.dispatches == [attempt.id]
    assert adapter.publications == []
    with factory() as s:
        assert get_packet(s, ref_b).status is S.RUNNING
        assert s.scalar(select(func.count()).select_from(ChannelRun)) == 1


# --------------------------------------------------------------------------- parked runs


def _parked_run(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as s:
        packet, run = claimed_run(s)
        attempt = authorise_dispatch(s, run, lock=held(s))
        move_run(s, run, R.AWAITING_RESULT, KERNEL, "launched")
        record_outcome(s, attempt, AttemptOutcome.PARKED, exit_code=3)
        confirmation = park_run(s, run, lock=held(s))
        s.commit()
        return packet.ref, run.id, confirmation.id


def test_approval_and_rejection_race_for_a_parked_run(factory: sessionmaker[Session]) -> None:
    ref, run_id, confirmation_id = _parked_run(factory)

    def approve() -> str:
        with factory() as s:
            approve_run(s, confirmation_id, actor=USER)
            s.commit()
            return "approved"

    def reject() -> str:
        with factory() as s:
            reject_run(s, confirmation_id, actor=USER, reason="no")
            s.commit()
            return "rejected"

    outcomes = _both(
        approve,
        reject,
        allowed_exceptions=(ConfirmationError, LostRace, OperationalError),
    )
    winners = [o for o in outcomes if o in ("approved", "rejected")]
    assert len(winners) == 1, outcomes
    assert all(
        o in {"approved", "rejected", "ConfirmationError", "LostRace", "OperationalError"}
        for o in outcomes
    )
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        packet = get_packet(s, ref)
        confirmation = s.get(Confirmation, confirmation_id)
        assert run is not None and confirmation is not None
        assert run.gate_revision == packet.gate_revision  # neither stales the run (R1, Q5)
        if winners == ["approved"]:
            assert confirmation.status == "approved"
            assert run.state is R.DISPATCHED and packet.status is S.RUNNING
            assert _effects(factory) == 2  # the first attempt plus exactly one resume
        else:
            assert confirmation.status == "rejected"
            assert run.state is R.AWAITING_CONFIRMATION and packet.status is S.HELD
            assert _effects(factory) == 1  # no resume


def test_a_late_approval_never_stales_a_rejected_parked_run(factory: sessionmaker[Session]) -> None:
    """The interleaving PostgreSQL found (READ COMMITTED): the approval reads the confirmation
    as pending and the run at G, then a rejection commits (moving packet and run to G + 1),
    then the approval reads the packet at G + 1. The torn read looks like a stale binding; the
    approval must lose (the confirmation is already decided), not stale the rejected run."""
    ref, run_id, confirmation_id = _parked_run(factory)
    packet_refreshes: list[int] = []

    with factory() as s:
        real_refresh = s.refresh

        def refresh(instance: object, *args: object, **kwargs: object) -> None:
            if isinstance(instance, Packet):
                packet_refreshes.append(1)
                if len(packet_refreshes) == 2:  # binding_mismatch: after the run, before this
                    with factory() as other:
                        reject_run(other, confirmation_id, actor=USER, reason="no")
                        other.commit()
            real_refresh(instance, *args, **kwargs)  # type: ignore[arg-type]

        s.refresh = refresh  # type: ignore[method-assign]
        with pytest.raises((ConfirmationError, OperationalError)):
            approve_run(s, confirmation_id, actor=USER)
            s.commit()  # a real caller commits a stale disposition; this one must not get here
    assert len(packet_refreshes) >= 2
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        confirmation = s.get(Confirmation, confirmation_id)
        packet = get_packet(s, ref)
        assert run is not None and confirmation is not None
        assert confirmation.status == "rejected"
        assert run.state is R.AWAITING_CONFIRMATION and packet.status is S.HELD
        assert packet.active_run_id == run.id
        assert run.gate_revision == packet.gate_revision
        assert _effects(factory) == 1


def test_approval_racing_a_governance_write_never_resumes(factory: sessionmaker[Session]) -> None:

    ref, run_id, confirmation_id = _parked_run(factory)

    def approve() -> str:
        with factory() as s:
            attempt = approve_run(s, confirmation_id, actor=USER)
            s.commit()
            return "approved" if attempt is not None else "stale"

    def waive() -> str:
        with factory() as s:
            grant_waiver(s, ref, "tools_needed", actor=USER, reason="x")
            s.commit()
            return "waived"

    outcomes = _both(
        approve,
        waive,
        allowed_exceptions=(LostRace, StaleRun, OperationalError),
    )
    assert set(outcomes) in (
        {"approved", "waived"},
        {"stale", "waived"},
        {"waived", "LostRace"},
        {"waived", "StaleRun"},
        {"waived", "OperationalError"},
    ), outcomes
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        assert run is not None
        if "approved" in outcomes:  # the approval committed first: one resume, then the waiver
            assert _effects(factory) == 2
            assert run.gate_revision != get_packet(s, ref).gate_revision  # later write: stale
        else:  # the waiver came first: the approval matched no gate and resumed nothing
            assert _effects(factory) == 1
            assert run.state in (R.STALE, R.AWAITING_CONFIRMATION)


# --------------------------------------------------------------------------- results


def test_a_duplicate_result_is_recorded_once(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        _, run = claimed_run(s)
        attempt_id = authorise_dispatch(s, run, lock=held(s)).id
        s.commit()

    def report() -> str:
        with factory() as s:
            attempt = s.get(ChannelRun, attempt_id)
            assert attempt is not None
            row = record_outcome(s, attempt, AttemptOutcome.SUCCEEDED, exit_code=0)
            s.commit()
            return "recorded" if row is not None else "duplicate"

    outcomes = _both(
        report,
        report,
        allowed_exceptions=(IntegrityError, OperationalError),
    )
    assert all(
        o in {"recorded", "duplicate", "IntegrityError", "OperationalError"} for o in outcomes
    )
    assert outcomes.count("recorded") == 1, outcomes
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(ChannelRunOutcome)) == 1


def test_a_late_result_after_lease_takeover_has_no_effect(factory: sessionmaker[Session]) -> None:
    from orchestrator.runs import reconcile_runs

    later = datetime.now(UTC) + timedelta(hours=2)
    with factory() as s:
        packet, run = claimed_run(s)
        old_attempt = authorise_dispatch(s, run, lock=held(s)).id
        s.commit()
        ref = packet.ref
    with factory() as s:  # kernel B: the lease expired; stale it and claim again (F = 2)
        reconcile_runs(s, now=later)
        packet = get_packet(s, ref)
        for status in (S.READY, S.QUEUED):
            transition(packet, status, KERNEL)
        new_run = claim(s, ref, "kernel-b", now=later)
        authorise_dispatch(s, new_run, now=later, lock=held(s))
        s.commit()

    def late() -> str:
        with factory() as s:
            attempt = s.get(ChannelRun, old_attempt)
            assert attempt is not None
            row = record_outcome(s, attempt, AttemptOutcome.SUCCEEDED, exit_code=0)
            s.commit()
            return str(row.outcome) if row is not None else "duplicate"

    outcomes = _both(
        late,
        late,
        allowed_exceptions=(IntegrityError, OperationalError),
    )
    # one report is recorded as stale evidence; the other is a duplicate (or loses the race)
    assert all(
        o in {"stale", "duplicate", "IntegrityError", "OperationalError"} for o in outcomes
    ), outcomes
    assert sorted(outcomes)[-1] == "stale" and outcomes.count("stale") == 1, outcomes
    with factory() as s:
        packet = get_packet(s, ref)
        assert packet.fencing_token == 2 and _effects(factory) == 2
        old = s.scalars(select(GenerationRun).where(GenerationRun.fencing_token == 1)).one()
        assert old.state is R.STALE and old.manifest_hash is None


# --------------------------------------------------------------------------- pause vs dispatch


def _planned(factory: sessionmaker[Session]) -> tuple[str, str]:
    with factory() as s:
        packet, run = claimed_run(s)
        s.commit()
        return run.id, packet.objective.ref


def test_dispatch_authorised_first_drains_when_the_pause_follows(
    factory: sessionmaker[Session],
) -> None:
    run_id, objective = _planned(factory)
    authorised, paused = threading.Event(), threading.Event()
    log: list[str] = []

    def dispatcher() -> None:
        with factory() as s:
            run = s.get(GenerationRun, run_id)
            assert run is not None
            authorise_dispatch(s, run, lock=held(s))  # scope rows touched and locked until commit
            authorised.set()
            paused.wait(1.5)  # SQLite waits behind the dispatch; PostgreSQL holds its row lock.
            s.commit()
            log.append("dispatch committed")

    def pauser() -> None:
        assert authorised.wait(15), "the dispatch was not authorised"
        with factory() as s:
            pause(s, "objective", objective, actor=USER, reason="stop")
            s.commit()
            log.append("pause committed")
        paused.set()

    _run_threads(dispatcher, pauser)
    assert log == ["dispatch committed", "pause committed"], log
    assert _effects(factory) == 1  # authorised before the pause committed: in flight, drains
    with factory() as s:
        assert scope_status(s, "objective", objective) is ScopeStatus.PAUSED
        run = s.get(GenerationRun, run_id)
        assert run is not None and run.state is R.DISPATCHED


def test_pause_committed_first_refuses_the_dispatch(factory: sessionmaker[Session]) -> None:
    run_id, objective = _planned(factory)
    pausing, done = threading.Event(), threading.Event()
    outcome: list[str] = []

    def pauser() -> None:
        with factory() as s:
            pause(s, "objective", objective, actor=USER, reason="stop")
            pausing.set()
            # Hold the uncommitted pause open while the dispatcher tries. SQLite refuses the
            # dispatch's write at once (database locked), so ``done`` is set; PostgreSQL makes
            # the dispatch wait on the scope row lock until this commit, so ``done`` cannot be
            # set first. The bounded hold is the barrier, not a failure condition.
            done.wait(1.0)
            s.commit()

    def dispatcher() -> None:
        assert pausing.wait(15), "the pause was not committed to the transaction"
        with factory() as s:
            try:
                run = s.get(GenerationRun, run_id)
                assert run is not None
                authorise_dispatch(s, run, lock=held(s))
                s.commit()
                outcome.append("dispatched")
            except (DispatchRefused, OperationalError) as exc:
                s.rollback()
                outcome.append(type(exc).__name__)
        done.set()

    _run_threads(pauser, dispatcher)
    assert outcome and outcome[0] in ("DispatchRefused", "OperationalError"), outcome
    assert _effects(factory) == 0
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        assert run is not None and run.state is R.PLANNED  # unchanged


# --------------------------------------------------------------------------- kernel lock


def test_two_kernels_race_for_an_expired_lock(factory: sessionmaker[Session]) -> None:
    start = datetime(2026, 11, 4, 12, tzinfo=UTC)
    with factory() as s:
        KernelLock("kernel-old").acquire(s, now=start)
        s.commit()
    later = start + timedelta(minutes=10)
    locks = {name: KernelLock(name) for name in ("kernel-a", "kernel-b")}

    def take(name: str) -> Callable[[], str]:
        def run() -> str:
            with factory() as s:
                locks[name].acquire(s, now=later)
                s.commit()
                return name

        return run

    outcomes = _both(
        take("kernel-a"),
        take("kernel-b"),
        allowed_exceptions=(LockHeld, OperationalError),
    )
    winners = [o for o in outcomes if o in locks]
    assert len(winners) == 1, outcomes
    assert all(o in {*locks, "LockHeld", "OperationalError"} for o in outcomes), outcomes
    loser = next(n for n in locks if n not in winners)
    with factory() as s:
        from orchestrator.control import FencedOut

        locks[winners[0]].fence(s, now=later)
        try:
            locks[loser].fence(s, now=later)
            raise AssertionError("the losing kernel must be fenced out")
        except FencedOut:
            pass
        assert s.scalar(select(func.count()).select_from(Packet)) == 0


# ----------------------------------------------------- admission and claim races (M2.1 fixes 2, 11)


def test_waiver_revoked_between_check_and_claim(factory: sessionmaker[Session]) -> None:
    ref, waiver_id = queued_ref(factory, waived=True)
    outcome = claim_race(
        factory,
        ref,
        lambda b: revoke(b, "waiver", waiver_id, actor=USER, reason="revoked mid-claim"),
    )
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0
        assert get_packet(s, ref).status is PacketStatus.QUEUED


def test_block_added_between_check_and_claim(factory: sessionmaker[Session]) -> None:
    ref, _ = queued_ref(factory, waived=False)
    outcome = claim_race(
        factory,
        ref,
        lambda b: add_override(
            b, ref, OverrideKind.BLOCK, actor=USER, reason="stop", scope="lineage"
        ),
    )
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0


def test_without_a_concurrent_write_the_claim_wins(factory: sessionmaker[Session]) -> None:
    ref, _ = queued_ref(factory, waived=False)
    assert claim_race(factory, ref, lambda b: None) == ["won"]
    with factory() as s:
        run = s.scalar(select(GenerationRun))
        assert run is not None and run.evaluation_snapshot["packet"]["gate_revision"] == 0


def test_concurrent_ticks_admit_once(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ref = ready_packet(s).ref
        s.commit()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def tick() -> None:
        with factory() as s:
            barrier.wait(10)
            try:
                admitted = admit_next(s)
                s.commit()
                results.append(admitted.ref if admitted else "none")
            except (ClaimError, LostRace, OperationalError) as exc:
                s.rollback()
                results.append(type(exc).__name__)

    _run_threads(tick, tick, timeout=30)
    assert results.count(ref) == 1
    assert all(
        result == ref or result in {"ClaimError", "LostRace", "OperationalError", "none"}
        for result in results
    )
    assert len(results) == 2
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(QueueAdmission)) == 1


# --------------------------------------------------------------------------- MR1 binding guard


def test_a_governance_write_after_the_binding_check_stops_the_dispatch(
    factory: sessionmaker[Session],
) -> None:
    """R4 MR1: session A has read and checked the bindings; session B commits a waiver (a gate
    bump) before A writes. A's binding guard is part of the write, so A dispatches nothing."""
    from orchestrator.governance import grant_waiver
    from orchestrator.runs import StaleRun

    with factory() as s:
        packet, run = claimed_run(s)
        s.commit()
        ref, run_id = packet.ref, run.id
    checked, written = threading.Event(), threading.Event()
    outcome: list[str] = []

    def dispatcher() -> None:
        with factory() as a:
            run = a.get(GenerationRun, run_id)
            assert run is not None

            def seam() -> None:
                checked.set()
                assert written.wait(15), "the writer never committed"

            try:
                authorise_dispatch(a, run, before_scopes=seam, lock=held(a))
                a.commit()
                outcome.append("dispatched")
            except StaleRun:
                a.rollback()
                outcome.append("stale")
            except OperationalError:  # SQLite: the snapshot is out of date (ruling 5)
                a.rollback()
                outcome.append("locked")

    wrote: list[str] = []

    def writer() -> None:
        assert checked.wait(15)
        try:
            with factory() as b:
                grant_waiver(b, ref, "tools_needed", actor=USER, reason="changed meanwhile")
                b.commit()
                wrote.append("committed")
        except OperationalError:  # SQLite: A's fence already holds the single write lock
            wrote.append("locked")
        finally:
            written.set()

    _run_threads(dispatcher, writer)
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        assert run is not None
        if wrote == ["committed"]:  # the gate moved after A's check: A must dispatch nothing
            assert outcome in (["stale"], ["locked"]), outcome
            assert _effects(factory) == 0 and run.state is R.PLANNED
        else:  # SQLite serialised the writer behind A: A's dispatch stands, the waiver never was
            assert wrote == ["locked"] and outcome == ["dispatched"], (wrote, outcome)
            assert _effects(factory) == 1 and run.state is R.DISPATCHED
            assert s.get_bind().dialect.name == "sqlite"
