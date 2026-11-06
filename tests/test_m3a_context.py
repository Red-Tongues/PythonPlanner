"""M3a (b): context links and ``question_context`` (AC11-AC16; R6 OQ4, OQ22, OQ23, OQ34)."""

from __future__ import annotations

import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestrator.contracts import PacketStatus, ProjectTree
from orchestrator.db.models import EvaContextLink, Event
from orchestrator.evaluation import evaluate_packet
from orchestrator.governance import add_dependency
from orchestrator.ingest import persist_tree
from orchestrator.llm import FakeProvider
from orchestrator.repository import update_objective, update_packet, update_project
from tests.conftest import USER
from tests.evafixtures import drive_to

BRIEF = "Every parser must stream its input in constant memory."
TREE = {
    "project": {"name": "Ctx", "brief": "A project about parsers and their limits."},
    "objectives": [
        {
            "name": "Build",
            "brief": BRIEF,
            "tasks": [
                {"title": "Write the parser", "prompt_description": "Parse things."},
                {"title": "Write the lexer", "prompt_description": "Lex things."},
            ],
        }
    ],
}
FIELD = "coding_intensity"


def _tree(session: Session):  # type: ignore[no-untyped-def]
    result = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER)
    session.commit()
    return result


def _quoting(quote: str) -> FakeProvider:
    answer = json.dumps(
        {"accuracy_score": 8, "evidence": [{"quote": quote, "tag": "SUPPORT"}], "reason": "ok"}
    )
    return FakeProvider().script(None, r"### TASK: evaluate", lambda _p: answer)


def _qc(session: Session, packet_id: str, field: str = FIELD) -> tuple[int, int, int]:
    row = session.execute(
        text(
            "SELECT question_context, question_context_count, question_weight_e4 "
            "FROM v_field_consensus WHERE packet_id = :id AND field_name = :f AND phase = 'pre'"
        ),
        {"id": packet_id, "f": field},
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _links(session: Session, packet_id: str) -> dict[str, EvaContextLink]:
    rows = session.scalars(
        select(EvaContextLink).where(
            EvaContextLink.packet_id == packet_id, EvaContextLink.field_name == FIELD
        )
    ).all()
    return {r.source_type if r.link_reason != "definition" else r.source_id: r for r in rows}


def test_input_hash_is_intra_packet(session: Session) -> None:
    """AC11."""
    tree = _tree(session)
    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=[FIELD])
    before = packet.input_hash
    update_project(session, tree.project, {"brief": "Changed."}, actor=USER)
    update_objective(session, tree.objectives[0], {"brief": "Changed too."}, actor=USER)
    assert packet.input_hash == before
    seen = {before}
    for change in (
        {"title": "T2"},
        {"prompt_description": "P2"},
        {"response_structure": "json_object"},
        {"repo_target_path": "src/p.py"},
    ):
        update_packet(session, packet, change, actor=USER)
        session.refresh(packet)
        assert packet.input_hash not in seen, change
        seen.add(packet.input_hash)


def test_links_are_registered_with_section_attribution(session: Session) -> None:
    """AC12: project, objective, one per dependency, definition and preset; a verified quote
    from the objective brief makes the objective link direct; without one it is peripheral."""
    tree = _tree(session)
    packet, other = tree.packets
    add_dependency(session, packet.ref, other.ref, actor=USER)
    provider = _quoting(BRIEF)
    evaluate_packet(session, packet, provider, fields=[FIELD])
    links = _links(session, packet.id)
    assert set(links) == {"project", "objective", "packet", FIELD, f"preset:{FIELD}:pre"}
    assert links["objective"].link_strength == "direct"
    assert links["objective"].link_reason == "direct_quote" and links["objective"].quote == BRIEF
    assert links["project"].link_strength == "peripheral"
    assert links["packet"].link_strength == "direct" and links["packet"].link_reason == "hard_dep"
    assert links["packet"].source_id == other.id

    evaluate_packet(session, other, FakeProvider(), fields=[FIELD])  # quotes the task only
    plain = _links(session, other.id)
    assert plain["objective"].link_strength == "peripheral"
    assert "packet" not in plain


def test_a_spanning_quote_marks_each_section(session: Session) -> None:
    tree = _tree(session)
    packet = tree.packets[0]
    spanning = "and their limits. OBJECTIVE: Build OBJECTIVE BRIEF: Every parser must"
    evaluate_packet(session, packet, _quoting(spanning), fields=[FIELD])
    links = _links(session, packet.id)
    assert links["project"].link_strength == "direct"
    assert links["objective"].link_strength == "direct"


def test_an_upstream_edit_questions_without_invalidating(session: Session) -> None:
    """AC13, AC14: results keep their value, status and currency; linked fields are
    questioned; no packet moves."""
    tree = _tree(session)
    packet = tree.packets[0]
    evaluate_packet(session, packet, _quoting(BRIEF), fields=[FIELD])
    status = packet.status
    before = session.execute(
        text(
            "SELECT final_value, status, is_current FROM v_field_consensus "
            "WHERE packet_id = :id AND field_name = :f"
        ),
        {"id": packet.id, "f": FIELD},
    ).one()
    assert _qc(session, packet.id) == (0, 0, 0)
    transitions = session.scalars(select(Event.id).where(Event.action == "packet.transition")).all()

    update_project(session, tree.project, {"brief": "A different project."}, actor=USER)
    after = session.execute(
        text(
            "SELECT final_value, status, is_current FROM v_field_consensus "
            "WHERE packet_id = :id AND field_name = :f"
        ),
        {"id": packet.id, "f": FIELD},
    ).one()
    assert tuple(after) == tuple(before)
    assert _qc(session, packet.id) == (1, 1, 250)  # the project link is peripheral
    update_objective(session, tree.objectives[0], {"name": "Build it"}, actor=USER)
    assert _qc(session, packet.id) == (1, 2, 750)  # the objective link is direct
    session.refresh(packet)
    assert packet.status == status
    assert (
        session.scalars(select(Event.id).where(Event.action == "packet.transition")).all()
        == transitions
    )


def test_a_broken_quote_is_questioned_by_the_drain(session: Session) -> None:
    """R6 OQ34: the drain re-verifies quotes against the edited source."""
    tree = _tree(session)
    packet = tree.packets[0]
    evaluate_packet(session, packet, _quoting(BRIEF), fields=[FIELD])
    update_objective(session, tree.objectives[0], {"name": "Renamed"}, actor=USER)
    assert not _links(session, packet.id)["objective"].is_questioned  # quote still there
    update_objective(session, tree.objectives[0], {"brief": "Something else."}, actor=USER)
    link = _links(session, packet.id)["objective"]
    session.refresh(link)
    assert link.is_questioned


def test_quiet_statuses_are_never_questioned(session: Session) -> None:
    """AC15."""
    tree = _tree(session)
    packet = tree.packets[0]
    evaluate_packet(session, packet, FakeProvider(), fields=[FIELD])
    update_project(session, tree.project, {"brief": "Changed."}, actor=USER)
    assert _qc(session, packet.id)[0] == 1
    drive_to(packet, PacketStatus.WAITING_FOR_CHILDREN)
    assert _qc(session, packet.id) == (0, 0, 0)


def test_reevaluation_clears_and_needs_no_model_call(session: Session) -> None:
    """AC16."""
    tree = _tree(session)
    packet = tree.packets[0]
    provider = FakeProvider()
    evaluate_packet(session, packet, provider, fields=[FIELD])
    update_project(session, tree.project, {"brief": "Changed."}, actor=USER)
    calls = len(provider.calls)
    assert _qc(session, packet.id)[0] == 1
    assert len(provider.calls) == calls  # computing question_context calls no model
    evaluate_packet(session, packet, provider, fields=[FIELD])
    assert _qc(session, packet.id) == (0, 0, 0)


def test_a_failed_reverification_rolls_back_the_edit(
    engine,
    session: Session,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """R6 OQ34/OQ35 (PM): re-verification runs inside the edit's transaction."""
    import pytest

    import orchestrator.evaluation.context as context

    tree = _tree(session)
    evaluate_packet(session, tree.packets[0], _quoting(BRIEF), fields=[FIELD])
    session.commit()
    objective_id = tree.objectives[0].id

    def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("injected verify failure")

    monkeypatch.setattr(context, "verify_quote", boom)
    with pytest.raises(RuntimeError, match="injected"):
        update_objective(session, tree.objectives[0], {"brief": "Gone."}, actor=USER)
    session.rollback()
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM raw_revision_outbox")).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM field_revisions")).scalar() == 0
        row = c.execute(
            text("SELECT content_revision, brief FROM objectives WHERE id = :id"),
            {"id": objective_id},
        ).one()
    assert tuple(row) == (0, BRIEF)
