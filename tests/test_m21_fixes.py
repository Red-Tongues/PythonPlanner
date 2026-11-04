"""Review R2 / M2.1 fix list (docs/reviews/R2_consensus.md): one section per fix.

Fixes 3, 5, 7 and 10 are also covered where their subject lives: the truth table
(TC-52 and exact-artifact binding), the lifecycle tests, and the parity tests (fixed point,
MC5's case, ruling 5). Every DB test runs on SQLite and, with ORCH_TEST_POSTGRES_URL, on
PostgreSQL; the immutability triggers stay on.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.confirmations import approve, reject
from orchestrator.contracts import (
    Channel,
    ConfirmationStatus,
    EvaPhase,
    OverrideKind,
    PacketStatus,
    ProjectTree,
    RunStatus,
)
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.models import (
    CandidateFieldDefinitionRow,
    ChannelRun,
    Confirmation,
    EvaluationCycle,
    EvaPacket,
    EvaPreset,
    Event,
    Exemplar,
    GenerationRun,
    Packet,
    QueueAdmission,
    new_id,
)
from orchestrator.errors import ImmutableRecordError
from orchestrator.evaluation import evaluate_packet
from orchestrator.evaluation.evaluator import EvaluationError
from orchestrator.events import record_event
from orchestrator.gates import guarded_status_update
from orchestrator.governance import add_dependency, add_override, grant_waiver, revoke
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider, ModelPinningError, ModelSpec, OllamaProvider
from orchestrator.llm.http import OpenAICompatibleProvider
from orchestrator.queue import ContextChanged, admit_next, claim, queue_rows
from orchestrator.repository import get_packet, set_setting, supersede_project
from tests.conftest import KERNEL, USER
from tests.evafixtures import Item, accept_all, add_artifact, seed_eva

TREE = {
    "project": {"name": "M2.1", "brief": "Review fixes."},
    "objectives": [
        {
            "name": "O",
            "tasks": [
                {"title": f"Task {i}", "prompt_description": f"Do thing number {i}."}
                for i in range(3)
            ],
        }
    ],
}
EIGHTS = {3: 8, 4: 8, 5: 8, 6: 8}
SUP = {4: (1, 0, 0), 5: (1, 0, 0)}


def _ready(session: Session, *, skip: frozenset[str] = frozenset()) -> Packet:
    packet = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets[0]
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet, skip=skip)
    transition(packet, PacketStatus.READY, KERNEL)
    return packet


def _raw(engine: Engine, sql: str, **params: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def _rejects(engine: Engine, sql: str, **params: object) -> None:
    with pytest.raises(Exception) as info:
        _raw(engine, sql, **params)
    assert is_immutability_violation(info.value), info.value


# --------------------------------------------------------------------------- fix 1


def test_fix1_admission_sends_a_stale_context_back_to_evaluation(session: Session) -> None:
    packet = _ready(session)
    project = session.get(type(packet.project), packet.project_id)
    supersede_project(session, project.ref, {"brief": "The brief changed."}, actor=USER)
    assert admit_next(session) is None
    assert packet.status is PacketStatus.EVALUATING_PRE
    event = session.scalar(select(Event).where(Event.action == "packet.context_changed"))
    assert event is not None and event.entity_id == packet.id


def test_fix1_claim_refuses_a_stale_context(session: Session) -> None:
    packet = _ready(session)
    assert admit_next(session).ref == packet.ref
    supersede_project(
        session, packet.project.ref, {"brief": "Changed after admission."}, actor=USER
    )
    with pytest.raises(ContextChanged):
        claim(session, packet.ref, "worker-1")
    session.commit()  # the audited revert is kept
    assert get_packet(session, packet.ref).status is PacketStatus.EVALUATING_PRE
    assert session.scalar(select(func.count()).select_from(GenerationRun)) == 0


def test_fix1_evaluation_writes_check_the_current_context(session: Session, tree) -> None:
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


# --------------------------------------------------------------------------- fix 2


def test_fix2_every_governance_writer_bumps_the_gate(session: Session) -> None:
    packet = _ready(session)
    other = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets[0]
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


def _race(factory: sessionmaker[Session], ref: str, writer) -> list[str]:  # type: ignore[no-untyped-def]
    """Session A checks and builds the snapshot; session B commits ``writer``; A's guarded
    UPDATE then runs. Returns A's outcome."""
    outcome: list[str] = []
    checked, written = threading.Event(), threading.Event()

    def worker_a() -> None:
        with factory() as a:

            def pause() -> None:
                checked.set()
                written.wait(10)

            try:
                claim(a, ref, "worker-a", before_cas=pause)
                a.commit()
                outcome.append("won")
            except Exception as exc:  # ClaimError, or SQLite's snapshot conflict
                a.rollback()
                outcome.append(type(exc).__name__)

    def worker_b() -> None:
        checked.wait(10)
        with factory() as b:
            writer(b)
            b.commit()
        written.set()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return outcome


def _queued(factory: sessionmaker[Session], *, waived: bool) -> tuple[str, str | None]:
    with factory() as s:
        skip = frozenset({"tools_needed"}) if waived else frozenset()
        packet = _ready(s, skip=skip)
        waiver_id = None
        if waived:
            seed_eva(
                s,
                packet,
                "tools_needed",
                {"other": Item("on", "on", {3: 4, 4: 4, 5: 4, 6: 4}, SUP)},
            )
            waiver_id = grant_waiver(s, packet.ref, "tools_needed", actor=USER, reason="ok").id
        assert admit_next(s).ref == packet.ref
        s.commit()
        return packet.ref, waiver_id


def test_fix2_waiver_revoked_between_check_and_claim(factory: sessionmaker[Session]) -> None:
    ref, waiver_id = _queued(factory, waived=True)
    outcome = _race(
        factory,
        ref,
        lambda b: revoke(b, "waiver", waiver_id, actor=USER, reason="revoked mid-claim"),
    )
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0
        assert get_packet(s, ref).status is PacketStatus.QUEUED


def test_fix2_block_added_between_check_and_claim(factory: sessionmaker[Session]) -> None:
    ref, _ = _queued(factory, waived=False)
    outcome = _race(
        factory,
        ref,
        lambda b: add_override(
            b, ref, OverrideKind.BLOCK, actor=USER, reason="stop", scope="lineage"
        ),
    )
    assert outcome and outcome[0] != "won"
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(GenerationRun)) == 0


def test_fix2_without_a_concurrent_write_the_claim_wins(factory: sessionmaker[Session]) -> None:
    ref, _ = _queued(factory, waived=False)
    assert _race(factory, ref, lambda b: None) == ["won"]
    with factory() as s:
        run = s.scalar(select(GenerationRun))
        assert run is not None and run.evaluation_snapshot["packet"]["gate_revision"] == 0


# --------------------------------------------------------------------------- fix 4


def _bump_definition(session: Session, name: str) -> None:
    row = session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == name)
    )
    row.prompt_hint += " (clarified)"
    row.version += 1
    record_event(session, actor=USER, action="t", entity_type="candidate_field_definition")
    session.flush()


def _field_status(session: Session, packet: Packet, name: str) -> str | None:
    return session.execute(
        text(
            "SELECT status FROM v_field_status WHERE packet_id = :id AND field_name = :f "
            "AND phase = 'pre'"
        ),
        {"id": packet.id, "f": name},
    ).scalar()


def test_fix4_definition_change_makes_results_pending_and_unreusable(session, tree) -> None:  # type: ignore[no-untyped-def]
    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    assert _field_status(session, packet, "coding_intensity") == "accepted"
    old = session.scalar(select(EvaPacket.content_address))
    _bump_definition(session, "coding_intensity")
    assert _field_status(session, packet, "coding_intensity") == "pending"
    report = evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    assert report.reused == [] and report.calls == 6
    assert (
        old
        not in session.scalars(
            select(EvaPacket.content_address).where(EvaPacket.id != EvaPacket.id)
        ).all()
    )
    assert _field_status(session, packet, "coding_intensity") == "accepted"


def test_fix4_exemplar_change_changes_the_content_address(session, tree) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.repository import supersede_packet

    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    session.add(
        Exemplar(field_name="coding_intensity", input_text="Write a parser.", expected_value=8)
    )
    record_event(session, actor=USER, action="t", entity_type="exemplar")
    session.flush()
    transition(packet, PacketStatus.NEEDS_REVIEW, KERNEL)
    v2 = supersede_packet(session, packet.ref, {"repo_target_path": "x.md"}, actor=USER)
    report = evaluate_packet(session, v2, FakeProvider(), fields=["coding_intensity"])
    assert report.reused == [] and report.calls == 6  # same text, new exemplars: no reuse


def test_fix4_new_preset_revision_makes_results_pending(session, tree) -> None:  # type: ignore[no-untyped-def]
    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    session.add(
        EvaPreset(
            id=new_id(),
            field_name="coding_intensity",
            phase="pre",
            revision=2,
            tier=2,
            required=True,
            hard=False,
            plan="combined",
            prompt_template="quickfire.v1",
            schema_version="1.0",
            policy_version="1.1",
            enabled=True,
        )
    )
    record_event(session, actor=USER, action="t", entity_type="eva_preset")
    session.flush()
    assert _field_status(session, packet, "coding_intensity") == "pending"


# --------------------------------------------------------------------------- fix 6


def test_fix6_cycle_bindings_and_eva_identity_are_frozen(engine: Engine, session, tree) -> None:  # type: ignore[no-untyped-def]
    eva = seed_eva(session, tree.packets[0], "coding_intensity", {"": Item("8", "8", EIGHTS, SUP)})
    session.commit()
    _raw(
        engine, "UPDATE evaluation_cycles SET state = 'complete' WHERE id = :id", id=eva.cycle_id
    )  # lifecycle columns may move
    _raw(engine, "UPDATE eva_packets SET state = 'extracting' WHERE id = :id", id=eva.id)
    for sql in (
        "UPDATE evaluation_cycles SET target_hash = 'x' WHERE id = :id",
        "UPDATE evaluation_cycles SET packet_version = 9 WHERE id = :id",
        "UPDATE evaluation_cycles SET models = '{\"x\": 1}' WHERE id = :id",
        "DELETE FROM evaluation_cycles WHERE id = :id",
    ):
        _rejects(engine, sql, id=eva.cycle_id)
    for sql in (
        "UPDATE eva_packets SET target_hash = 'x' WHERE id = :id",
        "UPDATE eva_packets SET content_address = 'x' WHERE id = :id",
        "UPDATE eva_packets SET model_config = '{\"x\": 1}' WHERE id = :id",
        "UPDATE eva_packets SET definition_version = 99 WHERE id = :id",
        "UPDATE eva_packets SET preset_id = cycle_id WHERE id = :id",
    ):
        _rejects(engine, sql, id=eva.id)
    session.expire_all()
    stored = session.get(EvaPacket, eva.id)
    stored.target_hash = "x"
    record_event(session, actor=KERNEL, action="t", entity_type="eva_packet", entity_id=eva.id)
    with pytest.raises(ImmutableRecordError, match="frozen from insert"):
        session.flush()
    session.rollback()
    cycle = session.get(EvaluationCycle, eva.cycle_id)
    cycle.pass_plan = "split@1"
    record_event(
        session, actor=KERNEL, action="t", entity_type="evaluation_cycle", entity_id=cycle.id
    )
    with pytest.raises(ImmutableRecordError, match="frozen from insert"):
        session.flush()


def test_fix6_dependencies_are_append_only(engine: Engine, session: Session, tree) -> None:
    dependency = add_dependency(session, tree.packets[0].ref, tree.packets[1].ref, actor=USER)
    session.commit()
    _rejects(engine, "UPDATE packet_dependencies SET kind = kind WHERE id = :id", id=dependency.id)
    _rejects(engine, "DELETE FROM packet_dependencies WHERE id = :id", id=dependency.id)


def test_fix6_postgres_truncate_guards(engine: Engine, tree) -> None:  # type: ignore[no-untyped-def]
    if engine.dialect.name != "postgresql":
        pytest.skip("TRUNCATE exists only on PostgreSQL")
    for table in (
        "generation_runs",
        "packet_dependencies",
        "evaluation_cycles",
        "eva_packets",
        "evaluation_pass_items",
        "queue_overrides",
        "waivers",
        "revocations",
        "queue_admissions",
        "branch_applications",
        "eva_presets",
    ):
        _rejects(engine, f"TRUNCATE {table} CASCADE")


def test_fix5_closed_unverified_must_be_locked(engine: Engine, tree) -> None:  # type: ignore[no-untyped-def]
    _rejects(
        engine,
        "UPDATE packets SET status = 'closed_unverified' WHERE id = :id",
        id=tree.packets[0].id,
    )


# --------------------------------------------------------------------------- fix 8


def _ollama(digest: str) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "eva-a", "digest": digest},
                        {"name": "eva-b", "digest": digest},
                    ]
                },
            )
        return httpx.Response(200, json={"model": "m", "message": {"content": "{}"}})

    return OllamaProvider(transport=httpx.MockTransport(handler))


def _actions(session: Session) -> set[str]:
    return set(session.scalars(select(Event.action).where(Event.action.like("evaluation.%"))))


def test_fix8_unpinned_or_unverifiable_evaluators_are_refused(session, tree) -> None:  # type: ignore[no-untyped-def]
    unpinned = {r: ModelSpec(f"eva-{r}") for r in ("a", "b", "c")}
    with pytest.raises(ModelPinningError, match="no pinned digest"):
        evaluate_packet(session, tree.packets[0], _ollama("d" * 64), specs=unpinned)
    pinned = {r: ModelSpec(f"eva-{r}", digest="d" * 64) for r in ("a", "b", "c")}
    with pytest.raises(ModelPinningError, match="cannot verify"):
        evaluate_packet(
            session, tree.packets[0], OpenAICompatibleProvider("http://x/v1"), specs=pinned
        )
    assert "evaluation.models_verified" not in _actions(session)
    assert "evaluation.model_pinning_failed" in _actions(session)


def test_fix8_pinned_and_verified_models_are_recorded(session, tree) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.evaluation.evaluator import Evaluator

    pinned = {r: ModelSpec(f"eva-{r}", digest="d" * 64) for r in ("a", "b", "c")}
    Evaluator(session, _ollama("sha256:" + "d" * 64), specs=pinned).verify_models()
    event = session.scalar(select(Event).where(Event.action == "evaluation.models_verified"))
    assert event is not None and event.after["digests"] == {"eva-a": "d" * 64, "eva-b": "d" * 64}


def test_fix8_fake_provider_is_never_recorded_as_verified(session, tree) -> None:  # type: ignore[no-untyped-def]
    evaluate_packet(session, tree.packets[0], FakeProvider(), fields=["coding_intensity"])
    assert "evaluation.models_verified" not in _actions(session)
    assert "evaluation.fake_provider" in _actions(session)


# --------------------------------------------------------------------------- fix 9


def test_fix9_unresolved_escalation_sends_any_tier_to_needs_review(session, tree) -> None:  # type: ignore[no-untyped-def]
    provider = FakeProvider().script(
        None, r"### TASK: extract\n### FIELD: web_independence", "no idea, sorry"
    )
    report = evaluate_packet(session, tree.packets[0], provider)
    assert report.unresolved == ["web_independence"]  # a tier-3 field
    assert report.status == "needs_review"


# --------------------------------------------------------------------------- fix 11


def test_fix11_admission_is_a_guarded_update(session: Session) -> None:
    packet = _ready(session)
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


def test_fix11_concurrent_ticks_admit_once(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        ref = _ready(s).ref
        s.commit()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def tick() -> None:
        with factory() as s:
            barrier.wait()
            try:
                admitted = admit_next(s)
                s.commit()
                results.append(admitted.ref if admitted else "none")
            except Exception:  # SQLite: the losing writer may get a snapshot conflict
                s.rollback()
                results.append("error")

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results.count(ref) == 1
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(QueueAdmission)) == 1


# --------------------------------------------------------------------------- fix 12


def _fail_runs(session: Session, packet: Packet, n: int, at: datetime) -> None:
    for i in range(n):
        session.add(
            ChannelRun(
                id=new_id(),
                run_id=f"LG-{new_id()[:8]}-{i}",
                channel=Channel.LOCAL_GENERATION,
                packet_id=packet.id,
                attempt=1,
                max_attempts=3,
                contract="x",
                status=RunStatus.FAILED,
                finished_at=at,
            )
        )
        record_event(session, actor=KERNEL, action="t", entity_type="channel_run")
        session.flush()


def _budget_confirmation(session: Session, packet: Packet) -> Confirmation:
    confirmation = session.scalar(
        select(Confirmation).where(
            Confirmation.entity_id == packet.id,
            Confirmation.entity_type == "failure_budget",
            Confirmation.status == ConfirmationStatus.PENDING,
        )
    )
    assert confirmation is not None
    return confirmation


def test_fix12_exhausted_budget_awaits_confirmation_then_approve(session: Session) -> None:
    packet = _ready(session)
    _fail_runs(session, packet, 3, datetime.now(UTC) - timedelta(hours=2))
    assert admit_next(session) is None
    assert packet.status is PacketStatus.AWAITING_CONFIRMATION
    event = session.scalar(
        select(Event).where(
            Event.action == "packet.transition",
            Event.entity_id == packet.id,
            Event.reason.like("lifetime failure budget%"),
        )
    )
    assert event is not None
    approve(
        session, _budget_confirmation(session, packet).id[:8], actor=USER, provider=FakeProvider()
    )
    assert packet.status is PacketStatus.READY
    row = next(r for r in queue_rows(session) if r.packet_ref == packet.ref)
    assert row.eligible and row.failed_runs == 0  # a fresh budget
    assert admit_next(session).ref == packet.ref


def test_fix12_reject_marks_the_packet_failed(session: Session) -> None:
    packet = _ready(session)
    set_setting(session, "queue.failure_budget", 1, actor=USER, reason="strict")
    _fail_runs(session, packet, 1, datetime.now(UTC) - timedelta(hours=2))
    admit_next(session)
    reject(session, _budget_confirmation(session, packet).id, actor=USER, reason="give up")
    assert packet.status is PacketStatus.FAILED


def test_post_evaluation_reads_the_newest_artifact(session: Session) -> None:
    packet = _ready(session)
    for status in (PacketStatus.QUEUED, PacketStatus.RUNNING, PacketStatus.EVALUATING_POST):
        transition(packet, status, KERNEL)
    with pytest.raises(EvaluationError, match="no artifact"):
        evaluate_packet(session, packet, FakeProvider(), phase=EvaPhase.POST)
    artifact = add_artifact(session, packet, "TASK: the generated output, short and correct.")
    report = evaluate_packet(
        session, packet, FakeProvider(), phase=EvaPhase.POST, fields=["correctness"]
    )
    targets = set(
        session.scalars(select(EvaPacket.target_hash).where(EvaPacket.phase == EvaPhase.POST))
    )
    assert report.fields and targets == {artifact.sha256}
