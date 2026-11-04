"""Requirement F / A1: immutability enforced by the database (raw SQL) and by the app guard."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orchestrator.contracts import AutonomyMode, Stage
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.models import Artifact, AutonomyScope, EvaluationPass, Event, utcnow
from orchestrator.errors import ImmutableRecordError
from orchestrator.events import record_event
from orchestrator.repository import delete_packet, get_packet, lock_record, update_packet
from tests.conftest import USER, make_cycle
from tests.evafixtures import complete_with_evidence


def raw(engine: Engine, sql: str, **params: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def assert_db_rejects(engine: Engine, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as info:
        raw(engine, sql, **params)
    assert is_immutability_violation(info.value), info.value


@pytest.fixture
def completed(tree, session: Session):  # type: ignore[no-untyped-def]
    packet = tree.packets[0]
    complete_with_evidence(session, packet, USER)  # the gated way (M2.1 fix 5)
    session.commit()
    return packet


# --------------------------------------------------------------------------- packets


def test_completed_packet_cannot_be_updated_or_deleted_at_db_level(engine, completed) -> None:
    assert_db_rejects(engine, "UPDATE packets SET title = 'hacked' WHERE id = :id", id=completed.id)
    assert_db_rejects(
        engine, "UPDATE packets SET locked = :f WHERE id = :id", id=completed.id, f=False
    )
    assert_db_rejects(engine, "UPDATE packets SET status = 'draft' WHERE id = :id", id=completed.id)
    assert_db_rejects(engine, "DELETE FROM packets WHERE id = :id", id=completed.id)


def test_unlocked_packet_can_be_updated_at_db_level(engine, tree) -> None:
    raw(engine, "UPDATE packets SET title = 'ok' WHERE id = :id", id=tree.packets[1].id)


def test_completion_requires_lock_on_update_and_insert(engine, tree) -> None:
    pid = tree.packets[1].id
    assert_db_rejects(engine, "UPDATE packets SET status = 'completed' WHERE id = :id", id=pid)
    assert_db_rejects(
        engine, "UPDATE packets SET status = 'archived', locked = :t WHERE id = :id", id=pid, t=True
    )  # completed_at missing
    p = tree.packets[1]
    insert = (
        "INSERT INTO packets (id, ref, project_id, objective_id, position, packet_kind, title, "
        "prompt_description, response_structure, response_contract, status, evaluations_count, "
        "manual_boost, locked, version, depth, source, created_at, updated_at) VALUES "
        "(:id, 'PP-999999', :project, :objective, 1, 'task', 't', 'd', 'markdown_document', "
        "'markdown_document@1.0', :status, 0, 0, :locked, 1, 0, 'test', :now, :now)"
    )
    common = {"project": p.project_id, "objective": p.objective_id, "now": utcnow().isoformat()}
    assert_db_rejects(engine, insert, id="x1", status="completed", locked=False, **common)
    raw(engine, insert, id="x2", status="draft", locked=False, **common)


def test_app_guard_raises_before_touching_locked_packet(session: Session, completed) -> None:
    packet = get_packet(session, completed.ref)
    with pytest.raises(ImmutableRecordError):
        update_packet(session, packet, {"title": "new"}, actor=USER)
    with pytest.raises(ImmutableRecordError):
        delete_packet(session, packet, actor=USER)
    # even a direct ORM edit is stopped at flush, before the database is asked
    packet.title = "sneaky"
    with pytest.raises(ImmutableRecordError):
        session.flush()


def test_unlocked_packet_update_and_delete_are_evented(session: Session, tree) -> None:
    packet = get_packet(session, tree.packets[1].ref)
    update_packet(session, packet, {"title": "Write the better CLI"}, actor=USER)
    delete_packet(session, get_packet(session, tree.packets[2].ref), actor=USER)
    session.commit()
    actions = [e.action for e in session.query(Event).filter(Event.entity_type == "packet")]
    assert "packet.updated" in actions and "packet.deleted" in actions


# --------------------------------------------------------------------------- projects/objectives


def test_locked_project_and_objective_reject_update_and_delete(engine, session, tree) -> None:
    lock_record(session, tree.project, actor=USER)
    lock_record(session, tree.objectives[1], actor=USER)
    session.commit()
    for table, row_id in (("projects", tree.project.id), ("objectives", tree.objectives[1].id)):
        assert_db_rejects(engine, f"UPDATE {table} SET name = 'x' WHERE id = :id", id=row_id)
        assert_db_rejects(
            engine, f"UPDATE {table} SET locked = :f WHERE id = :id", id=row_id, f=False
        )
        assert_db_rejects(engine, f"DELETE FROM {table} WHERE id = :id", id=row_id)
    with pytest.raises(ImmutableRecordError):
        lock_record(session, tree.project, actor=USER)


# --------------------------------------------------------------------------- artifacts


def test_accepted_artifact_is_immutable(engine, session: Session, tree) -> None:
    packet = tree.packets[1]
    draft = Artifact(packet_id=packet.id, path="a.md", sha256="0" * 64, content="v1")
    accepted = Artifact(
        packet_id=packet.id, path="b.md", sha256="1" * 64, accepted=True, accepted_at=utcnow()
    )
    session.add_all([draft, accepted])
    record_event(session, actor=USER, action="artifact.created", entity_type="artifact")
    session.commit()

    raw(engine, "UPDATE artifacts SET content = 'v2' WHERE id = :id", id=draft.id)
    assert_db_rejects(engine, "UPDATE artifacts SET content = 'x' WHERE id = :id", id=accepted.id)
    assert_db_rejects(
        engine, "UPDATE artifacts SET accepted = :f WHERE id = :id", id=accepted.id, f=False
    )
    assert_db_rejects(engine, "DELETE FROM artifacts WHERE id = :id", id=accepted.id)
    assert_db_rejects(
        engine, "UPDATE artifacts SET accepted = :t WHERE id = :id", id=draft.id, t=True
    )  # accepted_at missing

    session.expire_all()
    accepted = session.get(Artifact, accepted.id)
    accepted.content = "sneaky"
    with pytest.raises(ImmutableRecordError):
        session.flush()


# --------------------------------------------------------------------------- append-only tables


def test_events_are_append_only(engine, session: Session, tree) -> None:
    event_id = session.query(Event.id).first()[0]
    assert_db_rejects(engine, "UPDATE events SET reason = 'x' WHERE id = :id", id=event_id)
    assert_db_rejects(engine, "DELETE FROM events WHERE id = :id", id=event_id)
    assert_db_rejects(engine, "DELETE FROM events")
    event = session.get(Event, event_id)
    event.reason = "edited"
    with pytest.raises(ImmutableRecordError):
        session.flush()


def test_evaluation_passes_are_append_only(engine, session: Session, tree) -> None:
    ev = EvaluationPass(
        packet_id=tree.packets[0].id,
        field_name="coding_intensity",
        model="fake-a",
        pass_no=1,
        role="extract",
        value=7,
        score=None,
        eval_cycle_id=make_cycle(session, tree.packets[0].id),
    )
    session.add(ev)
    session.commit()
    assert_db_rejects(engine, "UPDATE evaluation_passes SET score = 1 WHERE id = :id", id=ev.id)
    assert_db_rejects(engine, "DELETE FROM evaluation_passes WHERE id = :id", id=ev.id)
    session.delete(ev)
    with pytest.raises(ImmutableRecordError):
        session.flush()


def test_postgres_truncate_is_rejected(engine, tree) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("TRUNCATE exists only on PostgreSQL")
    assert_db_rejects(engine, "TRUNCATE events")


# --------------------------------------------------------------------------- autonomy scopes


def test_fixed_autonomy_scope_cannot_change(engine, session: Session) -> None:
    stage = Stage.OBJECTIVE_LLM_VALUES.value
    assert_db_rejects(engine, "UPDATE autonomy_scopes SET mode = 'auto' WHERE stage = :s", s=stage)
    assert_db_rejects(
        engine, "UPDATE autonomy_scopes SET is_fixed = :f WHERE stage = :s", s=stage, f=False
    )
    assert_db_rejects(engine, "DELETE FROM autonomy_scopes WHERE stage = :s", s=stage)
    raw(engine, "UPDATE autonomy_scopes SET mode = 'auto' WHERE stage = 'ingestion'")
    session.expire_all()
    assert session.get(AutonomyScope, Stage.INGESTION).mode is AutonomyMode.AUTO
