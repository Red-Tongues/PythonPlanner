"""Queue: unique order, ageing with simulated time, eligibility, fairness, claims (B4, B9)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.contracts import (
    PacketStatus,
    ProjectTree,
)
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.models import (
    GenerationRun,
    Packet,
)
from orchestrator.errors import ImmutableRecordError
from orchestrator.gates import guarded_status_update
from orchestrator.governance import grant_waiver
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.queue import ClaimError, admit_next, claim, gen_queue_rows
from orchestrator.repository import get_packet, update_packet
from tests.conftest import KERNEL, USER
from tests.evafixtures import accept_all
from tests.govfixtures import (
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
    _ready(session)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    first = [r.packet_ref for r in gen_queue_rows(session, now)]
    assert first == [r.packet_ref for r in gen_queue_rows(session, now)]
    positions = [r.gen_position for r in gen_queue_rows(session, now)]
    assert positions == list(range(1, 6))


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
    update_packet(session, p, {"title": "changed at the last moment"}, actor=USER)
    with pytest.raises(ClaimError):  # its own results are no longer current
        claim(session, p.ref, "worker-1")


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


# ------------------------------------- guarded admission (M2.1 fix 11)


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
