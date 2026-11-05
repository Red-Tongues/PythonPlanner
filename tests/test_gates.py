"""Context binding and gate revisions (M2.1 fixes 1, 2): admission and claim run on current gates
only.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.contracts import (
    OverrideKind,
    PacketStatus,
    ProjectTree,
)
from orchestrator.db.models import (
    Event,
    GenerationRun,
)
from orchestrator.evaluation import evaluate_packet
from orchestrator.governance import add_dependency, add_override, grant_waiver, revoke
from orchestrator.ingest import persist_tree
from orchestrator.llm import FakeProvider
from orchestrator.queue import ContextChanged, admit_next, claim
from orchestrator.repository import get_packet, supersede_project
from tests.conftest import USER
from tests.govfixtures import (
    GOV_TREE,
    ready_packet,
)

# ---------------------------------------------------------------- context binding and gate revision


def test_admission_sends_a_stale_context_back_to_evaluation(session: Session) -> None:
    packet = ready_packet(session)
    project = session.get(type(packet.project), packet.project_id)
    supersede_project(session, project.ref, {"brief": "The brief changed."}, actor=USER)
    assert admit_next(session) is None
    assert packet.status is PacketStatus.EVALUATING_PRE
    event = session.scalar(select(Event).where(Event.action == "packet.context_changed"))
    assert event is not None and event.entity_id == packet.id


def test_claim_refuses_a_stale_context(session: Session) -> None:
    packet = ready_packet(session)
    assert admit_next(session).ref == packet.ref
    supersede_project(
        session, packet.project.ref, {"brief": "Changed after admission."}, actor=USER
    )
    with pytest.raises(ContextChanged):
        claim(session, packet.ref, "worker-1")
    session.commit()  # the audited revert is kept
    assert get_packet(session, packet.ref).status is PacketStatus.EVALUATING_PRE
    assert session.scalar(select(func.count()).select_from(GenerationRun)) == 0


def test_evaluation_writes_check_the_current_context(session: Session, tree) -> None:
    ref = tree.packets[0].ref
    project_ref = tree.project.ref
    changed: list[bool] = []

    def change_project_then_answer(prompt: str) -> str:
        if not changed:
            changed.append(True)
            supersede_project(session, project_ref, {"brief": "New context."}, actor=USER)
        return json.dumps({"accuracy_score": 8, "evidence": [], "reason": "ok"})

    provider = FakeProvider().script(None, r"### TASK: evaluate", change_project_then_answer)
    report = evaluate_packet(session, get_packet(session, ref), provider)
    assert report.stale  # the packet's input_hash was unchanged, but its context was not
    assert get_packet(session, ref).scores == {}


def test_every_governance_writer_bumps_the_gate(session: Session) -> None:
    packet = ready_packet(session)
    other = persist_tree(session, ProjectTree.model_validate(GOV_TREE), actor=USER).packets[0]
    seen = [packet.gate_revision]

    def bumped() -> None:
        session.refresh(packet)
        assert packet.gate_revision == seen[-1] + 1
        seen.append(packet.gate_revision)

    waiver = grant_waiver(session, packet.ref, "tools_needed", actor=USER, reason="ok")
    bumped()
    override = add_override(session, packet.ref, OverrideKind.PIN, actor=USER, reason="soon")
    bumped()
    revoke(session, "override", override.id, actor=USER, reason="never mind")
    bumped()
    revoke(session, "waiver", waiver.id, actor=USER, reason="never mind")
    bumped()
    dependency = add_dependency(session, packet.ref, other.ref, actor=USER)
    bumped()
    revoke(session, "dependency", dependency.id, actor=USER, reason="not needed")
    bumped()
    assert (
        sum(
            1
            for e in session.scalars(
                select(Event.after).where(
                    Event.action == "packet.gate_revision", Event.entity_id == packet.id
                )
            )
        )
        == 6
    )
