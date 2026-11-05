"""Queue: unique order, ageing with simulated time, eligibility, fairness, claims (B4, B9)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.confirmations import ConfirmationError, approve, reject
from orchestrator.contracts import (
    Channel,
    ConfirmationStatus,
    OverrideKind,
    PacketStatus,
    ProjectTree,
    RunStatus,
)
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.models import (
    ChannelRun,
    Event,
    GenerationRun,
    Packet,
    QueueAdmission,
    new_id,
)
from orchestrator.errors import ImmutableRecordError
from orchestrator.events import record_event
from orchestrator.gates import guarded_status_update
from orchestrator.governance import add_override, grant_waiver
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider
from orchestrator.queue import ClaimError, admit_next, claim, queue_rows
from orchestrator.repository import get_packet, set_setting, supersede_packet
from tests.conftest import KERNEL, USER
from tests.evafixtures import accept_all
from tests.govfixtures import (
    budget_confirmation,
    fail_runs,
    ready_packet,
)

TREE = {
    "project": {"name": "Q"},
    "objectives": [
        {
            "name": "O",
            "tasks": [
                {"title": f"Task {i}", "prompt_description": f"Do thing {i}."} for i in range(5)
            ],
        }
    ],
}


def _ready(session: Session, n: int = 5) -> list[Packet]:
    packets = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets[:n]
    for p in packets:
        transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
        accept_all(session, p)
        transition(p, PacketStatus.READY, KERNEL)
    return packets


def test_order_is_unique_and_deterministic(session: Session) -> None:
    packets = _ready(session)
    now = datetime.now(UTC)
    first = [r.packet_ref for r in queue_rows(session, now)]
    assert first == [r.packet_ref for r in queue_rows(session, now)]
    positions = [r.position for r in queue_rows(session, now)]
    assert positions == list(range(1, len(packets) + 1))
    # equal priority → tie broken by created_at then ref
    assert first == sorted(first)


def test_ageing_saturates_and_max_wait_serves_first(session: Session) -> None:
    packets = _ready(session, 2)
    base = datetime.now(UTC)
    early = {r.packet_ref: r for r in queue_rows(session, base)}
    later = {r.packet_ref: r for r in queue_rows(session, base + timedelta(hours=24))}
    much_later = {r.packet_ref: r for r in queue_rows(session, base + timedelta(hours=1000))}
    capped = {r.packet_ref: r for r in queue_rows(session, base + timedelta(hours=2000))}
    ref = packets[0].ref
    assert early[ref].priority < later[ref].priority < much_later[ref].priority
    assert much_later[ref].priority == capped[ref].priority  # saturates at max_age_hours
    assert later[ref].queue_class == "normal" and much_later[ref].queue_class == "max_wait"


def test_failure_budget_and_backoff(session: Session) -> None:
    packets = _ready(session, 2)
    target = packets[0]
    now = datetime.now(UTC)
    for i in range(3):
        session.add(
            ChannelRun(
                id=new_id(),
                run_id=f"LG{i}",
                channel=Channel.LOCAL_GENERATION,
                packet_id=target.id,
                attempt=1,
                max_attempts=3,
                contract="x",
                status=RunStatus.FAILED,
                finished_at=now - timedelta(minutes=10),
            )
        )
        record_event(session, actor=KERNEL, action="t", entity_type="channel_run")
        session.flush()
        row = next(r for r in queue_rows(session, now) if r.packet_ref == target.ref)
        assert row.ineligible_reason == ("failure_budget" if i == 2 else "backoff")
    row = next(
        r for r in queue_rows(session, now + timedelta(hours=5)) if r.packet_ref == target.ref
    )
    assert row.ineligible_reason == "failure_budget" and row.position is None


def test_override_precedence_and_version_binding(session: Session) -> None:
    packets = _ready(session, 3)
    a, b, c = packets
    add_override(session, c.ref, OverrideKind.PIN, actor=USER, reason="soon")
    add_override(session, b.ref, OverrideKind.FORCE, actor=USER, reason="now")
    order = [r.packet_ref for r in queue_rows(session) if r.eligible]
    assert order[:2] == [b.ref, c.ref]
    add_override(session, b.ref, OverrideKind.BLOCK, actor=USER, reason="stop")  # BLOCK > FORCE
    row = next(r for r in queue_rows(session) if r.packet_ref == b.ref)
    assert row.ineligible_reason == "blocked"
    # PIN lapses on revision (version-bound)
    transition(c, PacketStatus.HELD, USER, "revise it")
    v2 = supersede_packet(session, c.ref, {"title": "Task 2 again"}, actor=USER)
    transition(v2, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, v2)
    transition(v2, PacketStatus.READY, KERNEL)
    row = next(r for r in queue_rows(session) if r.packet_ref == c.ref)
    assert row.packet_version == 2 and row.queue_class == "normal"


def test_fair_admission_under_constant_pinning(session: Session) -> None:
    packets = _ready(session, 5)
    for p in packets[:4]:
        add_override(session, p.ref, OverrideKind.PIN, actor=USER, reason="mine first")
    classes = []
    for _ in range(5):
        admitted = admit_next(session)
        assert admitted is not None
        classes.append(
            session.scalars(
                select(QueueAdmission.admission_class).order_by(QueueAdmission.created_at.desc())
            ).first()
        )
    assert classes == ["pin", "pin", "pin", "normal", "pin"]


def test_claim_rechecks_and_freezes_the_snapshot(engine: Engine, session: Session) -> None:
    p = _ready(session, 1)[0]
    assert admit_next(session).ref == p.ref
    run = claim(session, p.ref, "worker-1")
    session.commit()
    assert get_packet(session, p.ref).status is PacketStatus.RUNNING
    assert run.evaluation_snapshot["packet"]["version"] == 1
    assert {c["field_name"] for c in run.evaluation_snapshot["consensus"]} >= {"safety_clear"}
    with pytest.raises(Exception) as info, engine.begin() as c:
        c.execute(text("UPDATE generation_runs SET evaluation_snapshot = '{}'"))
    assert is_immutability_violation(info.value)
    run.evaluation_snapshot = {}
    with pytest.raises(ImmutableRecordError):  # ORM guard too
        session.flush()


def test_edit_just_before_claim_loses(session: Session) -> None:
    p = _ready(session, 1)[0]
    admit_next(session)
    supersede_packet(session, p.ref, {"title": "changed at the last moment"}, actor=USER)
    with pytest.raises(ClaimError):
        claim(session, f"{p.ref}@1", "worker-1")


def test_two_workers_one_claim(engine: Engine, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        p = _ready(s, 1)[0]
        admit_next(s)
        s.commit()
        ref = p.ref
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with factory() as s:
            barrier.wait()
            try:
                claim(s, ref, name)
                s.commit()
                results.append("won")
            except Exception:  # ClaimError, or a lock conflict surfaced by the database
                s.rollback()
                results.append("lost")

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["lost", "won"]
    with factory() as s:
        assert len(s.scalars(select(GenerationRun)).all()) == 1


# ------------------------------------- guarded admission and the failure budget (M2.1 fixes 11, 12)


def test_admission_is_a_guarded_update(session: Session) -> None:
    packet = ready_packet(session)
    stale_gate = packet.gate_revision
    grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="bumps the gate")
    assert not guarded_status_update(
        session,
        packet,
        expected=PacketStatus.READY,
        observed_gate=stale_gate,
        values={"status": PacketStatus.QUEUED},
    )
    assert get_packet(session, packet.ref).status is PacketStatus.READY


def test_exhausted_budget_awaits_confirmation_then_approve(session: Session) -> None:
    packet = ready_packet(session)
    fail_runs(session, packet, 3, datetime.now(UTC) - timedelta(hours=2))
    assert admit_next(session) is None
    assert packet.status is PacketStatus.AWAITING_CONFIRMATION
    gate = packet.gate_revision
    event = session.scalar(
        select(Event).where(
            Event.action == "packet.transition",
            Event.entity_id == packet.id,
            Event.reason.like("lifetime failure budget%"),
        )
    )
    assert event is not None
    approve(
        session, budget_confirmation(session, packet).id[:8], actor=USER, provider=FakeProvider()
    )
    assert packet.status is PacketStatus.QUEUED  # D8: back to the queue; the claim re-checks
    assert packet.gate_revision == gate + 1
    row = next(r for r in queue_rows(session) if r.packet_ref == packet.ref)
    assert row.eligible and row.failed_runs == 0  # a fresh budget


def test_failure_budget_approval_rejects_a_stale_packet_version(session: Session) -> None:
    packet = ready_packet(session)
    fail_runs(session, packet, 3, datetime.now(UTC) - timedelta(hours=2))
    admit_next(session)
    confirmation = budget_confirmation(session, packet)
    session.execute(
        text("UPDATE confirmations SET entity_version = :version WHERE id = :id"),
        {"version": packet.version + 1, "id": confirmation.id},
    )
    session.refresh(confirmation)
    gate = packet.gate_revision

    with pytest.raises(ConfirmationError, match="targets v"):
        approve(session, confirmation.id, actor=USER)

    assert confirmation.status is ConfirmationStatus.PENDING
    assert packet.status is PacketStatus.AWAITING_CONFIRMATION
    assert packet.gate_revision == gate


def test_reject_holds_the_packet(session: Session) -> None:
    """C27 supersedes M2.1 #12's rejection outcome: rejecting holds the packet, with the reason."""
    packet = ready_packet(session)
    set_setting(session, "queue.failure_budget", 1, actor=USER, reason="strict")
    fail_runs(session, packet, 1, datetime.now(UTC) - timedelta(hours=2))
    admit_next(session)
    reject(session, budget_confirmation(session, packet).id, actor=USER, reason="give up")
    assert packet.status is PacketStatus.HELD
