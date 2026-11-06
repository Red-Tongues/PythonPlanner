"""The evaPacket evaluator end to end with FakeProvider (M2 §3, §8; B1, B3, B7, B9, B12)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from orchestrator.contracts import (
    EvaPhase,
    EvaState,
    PacketStatus,
    RunStatus,
)
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.models import (
    ChannelRun,
    Confirmation,
    EvaluationPass,
    EvaluationPassItem,
    EvaPacket,
    EvaPreset,
    Exemplar,
    Packet,
    new_id,
)
from orchestrator.evaluation import evaluate_packet, evaluate_pending, pending_packets
from orchestrator.evaluation.evaluator import EvaluationError
from orchestrator.evaluation.normalise import extract_json, normalise_quote, verify_quote
from orchestrator.events import record_event
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider, ModelSpec
from orchestrator.repository import (
    get_packet,
    update_packet,
)
from tests.conftest import KERNEL, USER
from tests.evafixtures import (
    TEXT_FIELD,
    add_artifact,
    ensure_text_field,
)
from tests.govfixtures import (
    bump_definition,
    field_status,
    ready_packet,
)
from tests.legacy import supersede_packet

PRE_FIELDS = 15  # task-level fields with pre-phase presets


def _evaluate(session: Session, ref: str, provider: FakeProvider, **kw):  # type: ignore[no-untyped-def]
    return evaluate_packet(session, get_packet(session, ref), provider, **kw)


def test_full_six_pass_run(session: Session, tree) -> None:
    provider = FakeProvider()
    ref = tree.packets[0].ref
    report = _evaluate(session, ref, provider)
    session.commit()
    packet = get_packet(session, ref)
    assert report.calls == PRE_FIELDS * 6 and report.status == "ready"
    assert set(report.fields.values()) == {"consensus"}
    assert packet.status is PacketStatus.READY and packet.evaluations_count == 1
    assert packet.scores["safety_clear"] is True and "coding_intensity" in packet.scores
    assert packet.tags  # tag-set projections
    passes = session.scalar(select(func.count()).select_from(EvaluationPass))
    runs = session.scalars(select(ChannelRun.run_id)).all()
    assert passes == PRE_FIELDS * 6 and len(runs) == PRE_FIELDS * 6
    assert all(r.startswith("QE") for r in runs)
    statuses = (
        session.execute(
            text("SELECT DISTINCT status FROM v_field_status WHERE packet_id = :id"),
            {"id": packet.id},
        )
        .scalars()
        .all()
    )
    assert set(statuses) == {"accepted"}


def test_calls_are_batched_model_a_then_model_b(session: Session, tree) -> None:
    provider = FakeProvider()
    _evaluate(session, tree.packets[0].ref, provider)
    sequence = [(c.model, c.task) for c in provider.calls]
    # per tier: A-extracts, B-extracts, A-evaluations, B-evaluations — the model switches
    # exactly 4 times per tier (3 tiers), never interleaving call by call
    switches = sum(1 for a, b in zip(sequence, sequence[1:], strict=False) if a[0] != b[0])
    assert switches == 3 * 4 - 1
    assert all(m == "fake-a" for m, t in sequence[: sequence.index(("fake-b", "extract"))])


def test_retry_with_a_concise_correction_then_success(session: Session, tree) -> None:
    provider = FakeProvider().fail_next(1)
    report = _evaluate(session, tree.packets[0].ref, provider)
    invalid = session.scalars(
        select(ChannelRun).where(ChannelRun.status == RunStatus.INVALID)
    ).all()
    assert len(invalid) == 1 and "\n" not in invalid[0].error
    retry_prompt = provider.calls[1].prompt
    assert "Your previous answer was invalid:" in retry_prompt and "Traceback" not in retry_prompt
    assert report.status == "ready"


def test_validation_failure_escalates_to_split_then_needs_review(session: Session, tree) -> None:
    provider = FakeProvider().script(
        None, r"### TASK: extract\n### FIELD: ambiguity_quotient", "I think it is fairly clear."
    )
    ref = tree.packets[0].ref
    report = _evaluate(session, ref, provider)
    session.commit()
    evas = session.scalars(
        select(EvaPacket)
        .where(EvaPacket.field_name == "ambiguity_quotient")
        .order_by(EvaPacket.escalation)
    ).all()
    assert [(e.plan.value, e.escalation, e.state) for e in evas] == [
        ("combined", 0, EvaState.FAILED),
        ("split", 1, EvaState.FAILED),
    ]
    assert report.escalated == ["ambiguity_quotient"]
    # combined: 1 attempt + 1 targeted repair; split re-run: attempts.quickfire_evaluation (4)
    attempts = session.scalars(
        select(ChannelRun.max_attempts).where(ChannelRun.status == RunStatus.INVALID)
    ).all()
    assert sorted(set(attempts)) == [2, 4]
    packet = get_packet(session, ref)
    assert packet.status is PacketStatus.NEEDS_REVIEW
    # a tier-1 failure spawns nothing further (no tier 2/3 evaPackets)
    tiers = session.scalars(select(EvaPacket.field_name).distinct()).all()
    assert "tools_needed" not in tiers and "styles" not in tiers
    review = session.scalar(select(Confirmation).where(Confirmation.entity_id == packet.id))
    assert review is not None and review.entity_type == "packet_review"
    # dispute deadlock → needs_review with no evaluation loop
    assert packet.ref not in [p.ref for p in pending_packets(session)]
    assert packet.ref not in [r.packet_ref for r in evaluate_pending(session, provider)]


def test_tier1_dispute_escalates_once_with_split_prompts(session: Session, tree) -> None:
    provider = FakeProvider(score_offsets={"fake-b": 4}, accuracy=6)
    report = _evaluate(session, tree.packets[0].ref, provider)
    split_tasks = {c.task for c in provider.calls if c.field == "task_parsability"}
    assert {"accuracy", "evidence"} <= split_tasks  # the split plan was used
    assert "task_parsability" in report.escalated
    assert report.status == "needs_review"


def test_noisy_output_is_parsed_and_quotes_verify(session: Session, tree) -> None:
    report = _evaluate(session, tree.packets[0].ref, FakeProvider(noisy=True))
    assert report.status == "ready"
    supports = session.scalar(select(func.sum(EvaluationPassItem.supports)))
    unverified = session.scalar(select(func.sum(EvaluationPassItem.unverified)))
    assert supports and supports > 0 and unverified == 0


def test_fabricated_quotes_are_stored_but_never_count(session: Session, tree) -> None:
    fabricated = json.dumps(
        {
            "accuracy_score": 9,
            "reason": "sure",
            "evidence": [{"quote": "a sentence that is not in the document", "tag": "SUPPORT"}],
        }
    )
    provider = FakeProvider().script(
        None, r"### TASK: evaluate\n### FIELD: coding_intensity", fabricated
    )
    _evaluate(session, tree.packets[0].ref, provider)
    row = session.execute(
        text(
            "SELECT status, supports, unverified FROM v_field_consensus "
            "WHERE field_name = 'coding_intensity'"
        )
    ).one()
    assert (row.status, row.supports, row.unverified) == ("disputed", 0, 4)
    stored = session.scalars(
        select(EvaluationPass.evidence).where(
            EvaluationPass.field_name == "coding_intensity", EvaluationPass.pass_no == 4
        )
    ).first()
    assert stored and stored[0]["verified"] is False


def test_content_addressed_reuse(session: Session, tree) -> None:
    ref = tree.packets[0].ref
    _evaluate(session, ref, FakeProvider())
    # a governance-only change keeps the target hash: every evaPacket is reused, no model calls
    v2 = supersede_packet(session, ref, {"repo_target_path": "docs/x.md"}, actor=USER)
    provider = FakeProvider()
    report = evaluate_packet(session, v2, provider)
    assert report.calls == 0 and len(report.reused) == PRE_FIELDS and report.status == "ready"
    # a different model configuration is a different content address: no reuse
    v3 = supersede_packet(session, ref, {"repo_target_path": "docs/y.md"}, actor=USER)
    specs = {r: ModelSpec(name=f"fake-{r}", digest="d" * 64) for r in ("a", "b", "c")}
    report = evaluate_packet(session, v3, FakeProvider(), specs=specs)
    assert report.reused == [] and report.calls == PRE_FIELDS * 6


def test_edit_during_evaluation_marks_work_superseded(session: Session, tree) -> None:
    ref = tree.packets[0].ref
    edited: list[Packet] = []

    def edit_then_answer(prompt: str) -> str:
        if not edited:
            edited.append(
                supersede_packet(session, ref, {"title": "Write the parser (v2)"}, actor=USER)
            )
        return json.dumps({"accuracy_score": 8, "evidence": [], "reason": "ok"})

    provider = FakeProvider().script(None, r"### TASK: evaluate", edit_then_answer)
    report = _evaluate(session, ref, provider)
    assert report.stale
    superseded = session.scalars(
        select(EvaPacket).where(EvaPacket.state == EvaState.SUPERSEDED)
    ).all()
    assert len(superseded) == 1
    old = session.get(Packet, superseded[0].packet_id)
    assert old is not None and old.version == 1 and old.scores == {}  # nothing projected
    # the new version is untouched by the stale work and evaluates normally
    assert evaluate_packet(session, edited[0], FakeProvider()).status == "ready"


def test_text_fields_take_four_passes(session: Session, tree) -> None:
    ensure_text_field(session)
    provider = FakeProvider()
    report = _evaluate(session, tree.packets[0].ref, provider, fields=[TEXT_FIELD])
    assert report.calls == 4 and report.fields == {TEXT_FIELD: "consensus"}
    passes = session.scalars(select(EvaluationPass.pass_no).order_by(EvaluationPass.pass_no))
    assert list(passes) == [1, 2, 3, 4]


def test_pass_items_are_append_only(engine: Engine, session: Session, tree) -> None:
    _evaluate(session, tree.packets[0].ref, FakeProvider(), fields=["coding_intensity"])
    session.commit()
    item_id = session.scalar(select(EvaluationPassItem.id))
    for sql in (
        "UPDATE evaluation_pass_items SET score = 0 WHERE id = :id",
        "DELETE FROM evaluation_pass_items WHERE id = :id",
        "UPDATE eva_packets SET state = 'planned' WHERE state = 'consensus'",
    ):
        with pytest.raises(Exception) as info, engine.begin() as c:
            c.execute(text(sql), {"id": item_id})
        assert is_immutability_violation(info.value)


def test_core_edit_after_evaluation_is_in_place_and_makes_results_pending(
    session: Session, tree
) -> None:
    """M3a (R6 OQ21): a core edit of an evaluated packet keeps the row; its input hash
    changes, so its own results stop being current until re-evaluated."""
    packet = tree.packets[0]
    _evaluate(session, packet.ref, FakeProvider())
    update_packet(session, packet, {"title": "x"}, actor=USER)
    assert get_packet(session, packet.ref).id == packet.id
    assert field_status(session, packet, "coding_intensity") == "pending"
    update_packet(session, packet, {"manual_boost": 3}, actor=USER)


def test_new_content_eval_packets_are_rejected(engine: Engine, tree) -> None:
    with pytest.raises(Exception) as info, engine.begin() as c:
        c.execute(text("UPDATE packets SET packet_kind = packet_kind WHERE 1 = 0"))
        row = dict(c.execute(text("SELECT * FROM packets LIMIT 1")).mappings().one())
        row.update(
            id="00000000-0000-0000-0000-000000000001",
            ref="PP-999999",
            packet_kind="content_eval",
            supersedes_id=None,
            version=1,
            tags="{}",
            scores="{}",
        )
        cols = ", ".join(row)
        c.execute(
            text(f"INSERT INTO packets ({cols}) VALUES ({', '.join(':' + k for k in row)})"), row
        )
    assert is_immutability_violation(info.value)


# --------------------------------------------------------------------------- B7 unit tests


def test_extract_json_tolerates_fences_preambles_and_trailing_commas() -> None:
    noisy = 'Sure!\n```json\n{"value": 7,}\n```\nanything else?'
    assert extract_json(noisy) == {"value": 7}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_quote_normalisation_is_conservative() -> None:
    source = "The user's **barcode** scan - via the phone_camera."
    assert verify_quote("The user’s barcode scan – via", source)
    assert verify_quote("phone_camera", source)  # identifiers keep their underscores
    assert not verify_quote("the users barcode", source)  # no fuzzy matching
    assert normalise_quote("  a \n *b*  ") == "a b"


# ---------------------------- currency of results, escalation, post-evaluation (M2.1 fixes 3, 4, 9)


def test_definition_change_makes_results_pending_and_unreusable(session, tree) -> None:  # type: ignore[no-untyped-def]
    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    assert field_status(session, packet, "coding_intensity") == "accepted"
    old = session.scalar(select(EvaPacket.content_address))
    bump_definition(session, "coding_intensity")
    assert field_status(session, packet, "coding_intensity") == "pending"
    report = evaluate_packet(session, packet, FakeProvider(), fields=["coding_intensity"])
    assert report.reused == [] and report.calls == 6
    assert (
        old
        not in session.scalars(
            select(EvaPacket.content_address).where(EvaPacket.id != EvaPacket.id)
        ).all()
    )
    assert field_status(session, packet, "coding_intensity") == "accepted"


def test_exemplar_change_changes_the_content_address(session, tree) -> None:  # type: ignore[no-untyped-def]
    from tests.legacy import supersede_packet

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


def test_new_preset_revision_makes_results_pending(session, tree) -> None:  # type: ignore[no-untyped-def]
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
    assert field_status(session, packet, "coding_intensity") == "pending"


def test_unresolved_escalation_sends_any_tier_to_needs_review(session, tree) -> None:  # type: ignore[no-untyped-def]
    provider = FakeProvider().script(
        None, r"### TASK: extract\n### FIELD: web_independence", "no idea, sorry"
    )
    report = evaluate_packet(session, tree.packets[0], provider)
    assert report.unresolved == ["web_independence"]  # a tier-3 field
    assert report.status == "needs_review"


def test_post_evaluation_reads_the_newest_artifact(session: Session) -> None:
    packet = ready_packet(session)
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
