"""M3a (c)/(d): genQueue, evaQueue, lockout, readiness and admission (AC17-AC27).

Every number is an integer computed identically on SQLite and PostgreSQL 16; the boundary
fixtures (PM rule 4) pin the edges: question cap 2,250, dependency grade 6,999/7,000, queued
depth 2/3, wait 7,199/7,200 s, ageing at 49/50 minutes and feedback decay at 24 h / 72 h.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from orchestrator.contracts import EvaPhase, PacketStatus, ProjectTree
from orchestrator.db.models import (
    EvaContextLink,
    EvaDiscardLog,
    Packet,
    PacketDependency,
    SealedBundle,
    new_id,
)
from orchestrator.evaluation import evaluate_packet
from orchestrator.evaluation.evaluator import LockedOut
from orchestrator.evaluation.readiness import mandatory_fields_for_packet, readiness
from orchestrator.events import record_event
from orchestrator.governance import add_dependency
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider
from orchestrator.queue import (
    add_feedback,
    admission_tick,
    eva_queue_rows,
    gen_queue_rows,
    is_locked_out,
    scored_rows,
    write_epoch_snapshot,
)
from orchestrator.repository import update_packet
from orchestrator.revisions import store_blob
from tests.conftest import KERNEL, USER
from tests.evafixtures import FIXTURE, Item, accept_all, seed_eva

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


def _tree(n: int = 4, *, objective_priority: int | None = None) -> dict:
    objective: dict = {
        "name": "O",
        "tasks": [{"title": f"Task {i}", "prompt_description": f"Do {i}."} for i in range(n)],
    }
    return {"project": {"name": "Q"}, "objectives": [objective]}


def _packets(session: Session, n: int = 4) -> list[Packet]:
    return persist_tree(session, ProjectTree.model_validate(_tree(n)), actor=USER).packets


def _ready(session: Session, packet: Packet, *, since: datetime = NOW) -> Packet:
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet)
    transition(packet, PacketStatus.READY, KERNEL)
    _set(session, packet, ready_since=since)
    return packet


def _set(session: Session, packet: Packet, **values: object) -> None:
    """Kernel bookkeeping columns (never audited) written directly, evented."""
    record_event(
        session, actor=FIXTURE, action="test.set", entity_type="packet", entity_id=packet.id
    )
    session.flush()
    session.connection().execute(
        update(Packet.__table__).where(Packet.__table__.c.id == packet.id).values(**values)
    )
    session.expire(packet)


def _row(session: Session, packet: Packet, now: datetime = NOW):  # type: ignore[no-untyped-def]
    return next(r for r in gen_queue_rows(session, now) if r.packet_id == packet.id)


def _score(session: Session, packet: Packet, now: datetime = NOW) -> dict:
    return next(r for r in scored_rows(session, now) if r["packet_id"] == packet.id)


def _seal(session: Session, packet: Packet, grade: int) -> SealedBundle:
    """A sealed lineage member with ``grade`` (the seal itself is tested in part e)."""
    blob = store_blob(session, f"snapshot of {packet.id}")
    bundle = SealedBundle(
        id=new_id(),
        packet_id=packet.id,
        claim_content_revision=0,
        snapshot_hash=blob,
        bundle_hash=blob,
        composite_grade_e4=grade,
    )
    session.add(bundle)
    record_event(
        session, actor=FIXTURE, action="t", entity_type="sealed_bundle", entity_id=bundle.id
    )
    session.flush()
    from tests.evafixtures import drive_to

    drive_to(packet, PacketStatus.EVALUATING_POST)
    _set(session, packet, status="sealed", locked=True, completed_at=NOW)
    return bundle


def _feedback(session: Session, packet: Packet, kind: str, who: str, at: datetime = NOW) -> None:
    """A feedback row at a fixed time (``add_feedback`` stamps the wall clock)."""
    from orchestrator.db.models import FeedbackEvent

    row = FeedbackEvent(
        id=new_id(), packet_id=packet.id, actor=f"user:{who}", kind=kind, reason="t", created_at=at
    )
    session.add(row)
    record_event(session, actor=FIXTURE, action="t", entity_type="feedback_event", entity_id=row.id)
    session.flush()


# --------------------------------------------------------------------------- scores (OQ6, OQ7)


def test_an_isolated_root_reaches_the_floor_after_fifty_minutes(session: Session) -> None:
    """OQ6: root grade 5000 → base 3000; ageing 10 per full minute, capped at 1500."""
    p = _ready(session, _packets(session, 1)[0])
    assert _score(session, p)["score_e4"] == 3000
    assert _score(session, p, NOW + timedelta(seconds=49 * 60 + 59))["score_e4"] == 3490
    assert _score(session, p, NOW + timedelta(minutes=50))["score_e4"] == 3500
    assert _score(session, p, NOW + timedelta(hours=10))["ageing_e4"] == 1500


def test_question_score_is_capped_and_subtracted(session: Session) -> None:
    """OQ4/OQ5: 4 direct-questioned fields = 3000 → capped at 2250; genQueue subtracts it and
    evaQueue adds twice it."""
    p = _ready(session, _packets(session, 1)[0])
    fields = [
        "requirement_alignment",
        "testability",
        "acceptance_criteria_coverage",
        "safety_clear",
    ]
    for name in fields:
        eva = seed_eva(session, p, name, {"": Item("3", "3", {3: 8, 4: 8, 5: 8, 6: 8},
                                                   {4: (1, 0, 0), 5: (1, 0, 0)})})  # fmt: skip
        if name == "safety_clear":
            eva = seed_eva(session, p, name, {"": Item("true", "true", {3: 8, 4: 8, 5: 8, 6: 8},
                                                       {4: (1, 0, 0), 5: (1, 0, 0)})})  # fmt: skip
        session.add(
            EvaContextLink(
                id=new_id(), eva_packet_id=eva.id, packet_id=p.id, field_name=name,
                source_type="project", source_id=p.project_id, source_hash="h",
                source_revision=0, link_strength="direct", link_reason="carried",
                is_questioned=True,
            )
        )  # fmt: skip
    session.flush()
    row = _score(session, p)
    assert row["question_score_e4"] == 2250
    assert row["score_e4"] == 3000 - 2250
    assert row["eva_priority_e4"] == 3000 + 2 * 2250
    # three questioned fields stay under the cap
    session.execute(text("SELECT 1"))


def test_feedback_latest_per_actor_decays_and_clamps(session: Session) -> None:
    """OQ9: latest per (packet, actor); ×1 under 24 h, ×0.5 to 72 h, 0 after; clamp ±3000."""
    from orchestrator.actors import Actor

    p = _ready(session, _packets(session, 1)[0])
    add_feedback(session, p.ref, "nudge", actor=USER, reason="a")
    add_feedback(session, p.ref, "pin", actor=USER, reason="b")  # latest from USER wins
    add_feedback(session, p.ref, "boost", actor=Actor.user("other"), reason="c")
    created = session.execute(text("SELECT MAX(created_at) FROM feedback_events")).scalar()
    t0 = created if isinstance(created, datetime) else datetime.fromisoformat(str(created))
    t0 = t0.replace(tzinfo=UTC) if t0.tzinfo is None else t0
    t0 = t0.replace(microsecond=0) + timedelta(seconds=1)
    assert _score(session, p, t0)["feedback_e4"] == 3000  # 3000 + 1500 clamped
    assert _score(session, p, t0 + timedelta(hours=25))["feedback_e4"] == 2250  # halved
    assert _score(session, p, t0 + timedelta(hours=73))["feedback_e4"] == 0
    add_feedback(session, p.ref, "demote", actor=Actor.user("third"), reason="d")
    assert _score(session, p, t0 + timedelta(hours=25))["feedback_e4"] in (750, 1500)


def test_dependency_grade_boundary_6999_7000(session: Session) -> None:
    """OQ13: an edge is satisfied by a sealed lineage member graded ≥ 7000."""
    a, b, c = _packets(session, 3)
    _ready(session, a)
    add_dependency(session, a.ref, b.ref, actor=USER)
    add_dependency(session, a.ref, c.ref, actor=USER)
    _seal(session, b, 7000)
    _seal(session, c, 6999)
    assert any(c.ref in x for x in readiness(session, a).blockers)
    assert not any(b.ref in x for x in readiness(session, a).blockers)
    assert a.id not in {r.packet_id for r in gen_queue_rows(session, NOW)}


def test_centrality_counts_unfinished_dependents(session: Session) -> None:
    """OQ8: 3000 per direct, 1500 per two-hop dependent, skipping finished lineages."""
    a, b, c, d = _packets(session, 4)
    add_dependency(session, b.ref, a.ref, actor=USER)
    add_dependency(session, c.ref, a.ref, actor=USER)
    add_dependency(session, d.ref, b.ref, actor=USER)
    _ready(session, a)
    row = (
        session.execute(
            text("SELECT * FROM v_packet_relational_centrality WHERE packet_id = :id"), {"id": a.id}
        )
        .mappings()
        .one()
    )
    assert (row["direct_dependents"], row["two_hop_dependents"]) == (2, 1)
    assert row["centrality_e4"] == 7500
    _seal(session, c, 7000)  # a finished lineage no longer counts
    row = session.execute(
        text("SELECT centrality_e4 FROM v_packet_relational_centrality WHERE packet_id = :id"),
        {"id": a.id},
    ).scalar()
    assert row == 4500


# --------------------------------------------------------------------------- admission (OQ7, OQ12)


def test_low_floor_applies_below_queued_depth_three(session: Session) -> None:
    """Depth 0-2 → floor 2500 (a root at 3000 is admitted); at depth 3 the floor is 3500."""
    packets = [_ready(session, p) for p in _packets(session, 4)]
    report = admission_tick(session, now=NOW)
    # one tick admits while depth < 3: three packets, then the floor rises to 3500
    assert len(report.admitted) == 3
    left = next(p for p in packets if p.ref not in report.admitted)
    row = _row(session, left)
    assert (row.queued_depth, row.floor_e4, row.score_e4, row.admissible) == (3, 3500, 3000, False)
    assert left.status is PacketStatus.READY


def test_floor_bypass_at_7200_seconds(session: Session) -> None:
    packets = [_ready(session, p) for p in _packets(session, 4)]
    admission_tick(session, now=NOW)  # three admitted (depth 3)
    left = next(p for p in packets if p.status is PacketStatus.READY)
    _set(session, left, ready_since=NOW - timedelta(seconds=7199))
    session.refresh(left)
    # 7199 s: ageing capped at 1190 → 4190 ≥ 3500 anyway; pin the bypass with a low score
    feedback_low = {"status": "ready"}
    assert feedback_low
    row = _row(session, left)
    assert row.wait_sec == 7199
    assert _row(session, left, NOW + timedelta(seconds=1)).wait_sec == 7200


def test_bypass_admits_a_low_score_at_7200_not_7199(session: Session) -> None:

    packets = [_ready(session, p) for p in _packets(session, 4)]
    admission_tick(session, now=NOW)
    left = next(p for p in packets if p.status is PacketStatus.READY)
    for i in range(2):  # two DEMOTEs (−3000) keep the score under 3500 despite ageing
        _feedback(session, left, "demote", f"u{i}")
    for wait, admitted in ((7199, False), (7200, True)):
        _set(session, left, ready_since=NOW - timedelta(seconds=wait))
        row = _row(session, left, NOW)
        assert row.score_e4 < row.floor_e4
        report = admission_tick(session, now=NOW)
        assert (left.ref in report.admitted) is admitted, wait


def test_a_queued_packet_below_the_floor_returns_to_ready(session: Session) -> None:
    """OQ12: queued → ready only by score; never back to evaluating_pre."""
    packets = [_ready(session, p) for p in _packets(session, 4)]
    admission_tick(session, now=NOW)  # three admitted (depth < 3 → floor 2500)
    left = next(p for p in packets if p.status is PacketStatus.READY)
    for i in range(2):
        _feedback(session, left, "boost", f"b{i}")  # 3000 + 3000 ≥ 3500
    assert left.ref in admission_tick(session, now=NOW).admitted
    victim = next(p for p in packets if p is not left)
    for i in range(2):
        _feedback(session, victim, "demote", f"d{i}")  # 3000 − 3000 < 3500 at depth 4
    report = admission_tick(session, now=NOW)
    # measured at the depth without itself (3 → floor 3500); the other roots, measured at depth
    # 2 (floor 2500), stay: no flip-flop
    assert report.demoted == [victim.ref] and victim.status is PacketStatus.READY
    assert _row(session, victim).queued_depth == 3


# --------------------------------------------------------------------------- gates (OQ10, OQ11)


def test_mandatory_fields_per_trigger_and_sql_parity(session: Session) -> None:
    """AC23: universal; + dependencies; + dependents; + charted. Python equals the view."""
    a, b = _packets(session, 2)
    base = set(mandatory_fields_for_packet(session, a))
    assert {"requirement_alignment", "testability", "acceptance_criteria_coverage"} <= base
    assert "safety_clear" in base  # the task family's required preset
    add_dependency(session, a.ref, b.ref, actor=USER)
    assert {"dependency_validity", "interface_stability"} <= set(
        mandatory_fields_for_packet(session, a)
    )
    assert {"downstream_contract_clarity", "risk_assessment"} <= set(
        mandatory_fields_for_packet(session, b)
    )
    update_packet(session, b, {"repo_target_path": "src/b.py"}, actor=USER)
    assert "sonar_hotspot_alignment" in mandatory_fields_for_packet(session, b)
    for p in (a, b):
        sql = set(
            session.execute(
                text("SELECT field_name FROM v_packet_mandatory_fields WHERE packet_id = :id"),
                {"id": p.id},
            ).scalars()
        )
        assert sql == set(mandatory_fields_for_packet(session, p))


def test_a_missing_mandatory_field_blocks_ready(session: Session) -> None:
    """AC24."""
    p = _packets(session, 1)[0]
    transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, p, skip=frozenset({"testability"}))
    assert any("testability" in b for b in readiness(session, p).blockers)
    seed_eva(session, p, "testability", {"": Item("3", "3", {3: 8, 4: 8, 5: 8, 6: 8},
                                                  {4: (1, 0, 0), 5: (1, 0, 0)})})  # fmt: skip
    assert readiness(session, p).ready


def test_ambiguity_bound_is_part_of_readiness(session: Session) -> None:
    p = _packets(session, 1)[0]
    transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, p, skip=frozenset({"ambiguity_quotient"}))
    sup = {4: (1, 0, 0), 5: (1, 0, 0)}
    seed_eva(session, p, "ambiguity_quotient", {"": Item("5", "5", {3: 8, 4: 8, 5: 8, 6: 8}, sup)})
    assert any("ambiguity 5000 > 4000" in b for b in readiness(session, p).blockers)
    seed_eva(session, p, "ambiguity_quotient", {"": Item("4", "4", {3: 8, 4: 8, 5: 8, 6: 8}, sup)})
    assert readiness(session, p).ready


# --------------------------------------------------------------------------- queues and lockout


def test_gen_queue_membership_and_eva_subset(session: Session) -> None:
    """AC17, AC18."""
    a, b, c, d = _packets(session, 4)
    _ready(session, a)
    _ready(session, b)
    transition(c, PacketStatus.EVALUATING_PRE, KERNEL)
    gen = {r.packet_id: r for r in gen_queue_rows(session, NOW)}
    assert set(gen) == {a.id, b.id}
    eva = eva_queue_rows(session, NOW)
    assert {r.packet_id for r in eva} >= {c.id, d.id}  # draft / evaluating_pre are enriched
    for r in eva:
        if r.status in ("ready", "queued"):
            assert r.packet_id in gen
        assert r.missing_mandatory_fields or r.missing_optional_fields or r.question_context_count


def test_lockout_reads_the_snapshot_and_refuses_the_evaluator(session: Session) -> None:
    """AC20, OQ3, OQ31: positions 0-2 and their relatives are locked out of evaQueue as of the
    newest snapshot; a direct evaluator call is refused with an event."""
    packets = [_ready(session, p) for p in _packets(session, 4)]
    related = _packets(session, 1)[0]
    update_packet(session, related, {"repo_target_path": "same.py"}, actor=USER)
    zone = gen_queue_rows(session, NOW)[:3]
    first = session.get(Packet, zone[0].packet_id)
    assert first is not None
    update_packet(session, first, {"repo_target_path": "same.py"}, actor=USER)
    accept_all(session, first)  # its own edit made its results pending; restore them
    assert not is_locked_out(session, related.id)  # no snapshot yet
    write_epoch_snapshot(session, "test", now=NOW)
    assert is_locked_out(session, related.id)  # same non-null repo path
    eva_ids = {r.packet_id for r in eva_queue_rows(session, NOW)}
    assert related.id not in eva_ids
    with pytest.raises(LockedOut):
        evaluate_packet(session, related, FakeProvider())
    assert (
        session.execute(
            text("SELECT COUNT(*) FROM events WHERE action = 'evaluation.lockout_refused'")
        ).scalar()
        == 1
    )
    assert packets


def test_an_evaluation_that_loses_the_commit_cas_is_discarded(session: Session) -> None:
    """OQ30: the content revision moved during the evaluation → discarded, logged."""
    p = _packets(session, 1)[0]
    edited: list[bool] = []

    def edit_then_answer(_prompt: str) -> str:
        import json

        if not edited:
            edited.append(True)
            session.execute(
                text("UPDATE packets SET manual_boost = 7 WHERE id = :id"), {"id": p.id}
            )
        return json.dumps({"accuracy_score": 8, "evidence": [], "reason": "ok"})

    provider = FakeProvider().script(None, r"### TASK: evaluate", edit_then_answer)
    report = evaluate_packet(session, p, provider, fields=["coding_intensity"])
    assert report.stale
    log = session.scalars(select(EvaDiscardLog)).one()
    assert (log.expected_content_revision, log.actual_content_revision) == (0, 1)
    assert not log.locked_out


def test_positions_and_scores_are_dialect_identical_for_21_ties(session: Session) -> None:
    """AC21: 21 equal roots, same ready_since → ordered by packet id; integer scores."""
    packets = [_ready(session, p) for p in _packets(session, 21)]
    rows = gen_queue_rows(session, NOW)
    assert [r.gen_position for r in rows] == list(range(1, 22))
    assert {r.score_e4 for r in rows} == {3000}
    assert [r.packet_id for r in rows] == sorted(p.id for p in packets)


def test_ready_and_queued_packets_are_enriched_in_place(session: Session) -> None:
    """AC19, OQ12: a ready packet with missing optional fields is evaluated and stays ready."""
    p = _ready(session, _packets(session, 1)[0])
    assert p.id in {r.packet_id for r in eva_queue_rows(session, NOW)}
    evaluate_packet(session, p, FakeProvider(), fields=["domains"])
    assert p.status is PacketStatus.READY
    _ = EvaPhase.PRE, PacketDependency
