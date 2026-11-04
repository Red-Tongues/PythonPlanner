"""Review R1 / M1.1 fix list (docs/reviews/R1_consensus.md): one section per fix.

Fix 4 (frozen migrations) is covered by ``test_migrations_frozen.py``. Every DB test here runs
on SQLite and, when ``ORCH_TEST_POSTGRES_URL`` is set, on PostgreSQL (A6).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from orchestrator.actors import Actor, ActorKind
from orchestrator.contracts import CandidateFieldDefinition, PacketStatus, ProjectTree
from orchestrator.db import init_db, make_engine, make_session_factory
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.migrate import alembic_config, current_revision, head_revision
from orchestrator.db.models import (
    EvaluationPass,
    Event,
    FieldRegistryRevision,
    Packet,
    Setting,
    Threshold,
    new_id,
)
from orchestrator.db.refs import allocate
from orchestrator.db.seed import current_registry_revision, record_registry_revision
from orchestrator.errors import ImmutableRecordError, InvalidTransitionError, UnauditedWriteError
from orchestrator.events import record_event
from orchestrator.fields import load_candidate_fields
from orchestrator.ingest import ingest_file, parse_text, persist_tree
from orchestrator.ingest.parser import DESCRIPTION_MAX, UNTITLED_TASK, split_text
from orchestrator.lifecycle import transition
from orchestrator.repository import (
    delete_packet,
    get_packet,
    supersede_objective,
    supersede_packet,
    supersede_project,
    update_packet,
)
from tests.conftest import KERNEL, SMALL_TREE, USER, make_cycle
from tests.evafixtures import complete_with_evidence
from tests.test_migrations_frozen import MANIFEST, fingerprint

LLM = Actor.llm("model-a")


def raw(engine: Engine, sql: str, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def assert_db_rejects(engine: Engine, sql: str, **params: Any) -> None:
    with pytest.raises(DBAPIError) as info:
        raw(engine, sql, **params)
    assert is_immutability_violation(info.value), info.value


def row_of(engine: Engine, table: str, row_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT * FROM {table} WHERE id = :id"), {"id": row_id})
        return dict(result.mappings().one())


def insert_row(engine: Engine, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    params = ", ".join(f":{c}" for c in values)
    values = {k: json.dumps(v) if isinstance(v, dict | list) else v for k, v in values.items()}
    raw(engine, f"INSERT INTO {table} ({columns}) VALUES ({params})", **values)


# --------------------------------------------------------------------------- fix 1


@pytest.fixture
def superseded(session: Session, tree):  # type: ignore[no-untyped-def]
    """v1 of a packet, objective and project, each superseded but NOT locked."""
    supersede_packet(session, tree.packets[1].ref, {"title": "v2"}, actor=USER)
    supersede_objective(session, tree.objectives[1].ref, {"name": "Ship v2"}, actor=USER)
    supersede_project(session, tree.project.ref, {"brief": "v2"}, actor=USER)
    session.commit()
    return {
        "packets": tree.packets[1].id,
        "objectives": tree.objectives[1].id,
        "projects": tree.project.id,
    }


@pytest.mark.parametrize("table", ["packets", "objectives", "projects"])
def test_fix1_superseded_rows_are_immutable_at_db_level(engine, superseded, table) -> None:
    column = "title" if table == "packets" else "name"
    assert_db_rejects(
        engine, f"UPDATE {table} SET {column} = 'x' WHERE id = :id", id=superseded[table]
    )
    assert_db_rejects(engine, f"DELETE FROM {table} WHERE id = :id", id=superseded[table])


def test_fix1_orm_guard_rejects_superseded_edits(session: Session, superseded) -> None:
    old = session.get(Packet, superseded["packets"])
    with pytest.raises(ImmutableRecordError, match="superseded"):
        update_packet(session, old, {"title": "x"}, actor=USER)
    old.title = "sneaky"
    record_event(session, actor=USER, action="t", entity_type="packet", entity_id=old.id)
    with pytest.raises(ImmutableRecordError, match="superseded"):
        session.flush()


# --------------------------------------------------------------------------- fix 2


def test_fix2_successor_must_keep_ref_version_and_ancestry(engine, tree) -> None:
    base = row_of(engine, "packets", tree.packets[0].id)
    good = {**base, "id": new_id(), "version": 2, "supersedes_id": base["id"], "locked": False}
    for bad in (
        {"version": 3},
        {"ref": "PP-999999"},
        {"root_id": tree.packets[1].id},
        {"objective_id": tree.objectives[1].id},
    ):
        assert_db_rejects_insert(engine, {**good, **bad})
    assert_db_rejects_insert(engine, {**base, "id": new_id(), "ref": "PP-999998", "version": 2})
    insert_row(engine, "packets", good)  # the valid successor is accepted
    with pytest.raises(IntegrityError):  # at most one successor per predecessor
        insert_row(engine, "packets", {**good, "id": new_id(), "version": 2})


def assert_db_rejects_insert(engine: Engine, values: dict[str, Any]) -> None:
    with pytest.raises(DBAPIError) as info:
        insert_row(engine, "packets", values)
    assert is_immutability_violation(info.value), info.value


@pytest.mark.parametrize(
    ("column", "value"),
    [("ref", "PP-777777"), ("version", 5), ("parent_id", None), ("root_id", None)],
)
def test_fix2_lineage_fields_never_change(engine, tree, column, value) -> None:
    pid = tree.packets[0].id
    if column == "parent_id":
        value = tree.packets[1].id
    assert_db_rejects(engine, f"UPDATE packets SET {column} = :v WHERE id = :id", v=value, id=pid)
    assert_db_rejects(
        engine,
        "UPDATE objectives SET project_id = :v WHERE id = :id",
        v=new_id(),
        id=tree.objectives[0].id,
    )


def test_fix2_superseding_versions_and_rows_with_dependents_cannot_be_deleted(
    engine, session: Session, tree
) -> None:
    v2 = supersede_packet(session, tree.packets[2].ref, {"title": "v2"}, actor=USER)
    session.commit()
    assert_db_rejects(engine, "DELETE FROM packets WHERE id = :id", id=v2.id)
    with pytest.raises(ImmutableRecordError, match="archive instead"):
        delete_packet(session, get_packet(session, v2.ref), actor=USER)

    # a child packet makes its parent undeletable (DB and repository)
    parent = tree.packets[0]
    child = {
        **row_of(engine, "packets", parent.id),
        "id": new_id(),
        "ref": "PP-900001",
        "parent_id": parent.id,
        "root_id": parent.root_id,
        "depth": 1,
    }
    insert_row(engine, "packets", child)
    assert_db_rejects(engine, "DELETE FROM packets WHERE id = :id", id=parent.id)
    session.expire_all()
    with pytest.raises(ImmutableRecordError, match="child packet PP-900001"):
        delete_packet(session, get_packet(session, parent.ref), actor=USER)

    assert_db_rejects(engine, "DELETE FROM objectives WHERE id = :id", id=tree.objectives[0].id)
    assert_db_rejects(engine, "DELETE FROM projects WHERE id = :id", id=tree.project.id)


def test_fix2_a_plain_v1_draft_can_still_be_deleted(session: Session, tree) -> None:
    delete_packet(session, get_packet(session, tree.packets[1].ref), actor=USER)
    session.commit()


# --------------------------------------------------------------------------- fix 3


def test_fix3_insert_or_replace_cannot_bypass_delete_triggers(engine, session, tree) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("INSERT OR REPLACE is SQLite syntax")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA recursive_triggers")).scalar() == 1

    packet = tree.packets[0]
    complete_with_evidence(session, packet, USER)
    session.commit()
    locked = row_of(engine, "packets", packet.id)
    replacement = {
        **locked,
        "title": "replaced",
        "status": "draft",
        "locked": False,
        "completed_at": None,
    }
    cols = ", ".join(replacement)
    assert_db_rejects(
        engine,
        f"INSERT OR REPLACE INTO packets ({cols}) VALUES "
        f"({', '.join(':' + c for c in replacement)})",
        **replacement,
    )

    event = row_of(engine, "events", session.scalar(select(Event.id)))
    event = {
        **event,
        "reason": "rewritten",
        "before": json.dumps(event["before"]) if event["before"] else None,
        "after": json.dumps(event["after"]) if event["after"] else None,
    }
    cols = ", ".join(event)
    assert_db_rejects(
        engine,
        f"INSERT OR REPLACE INTO events ({cols}) VALUES ({', '.join(':' + c for c in event)})",
        **event,
    )

    ev = EvaluationPass(
        packet_id=tree.packets[1].id,
        eval_cycle_id=make_cycle(session, tree.packets[1].id),
        field_name="styles",
        model="fake-a",
        pass_no=1,
        role="extract",
        value=["plan"],
    )
    session.add(ev)
    session.commit()
    stored = row_of(engine, "evaluation_passes", ev.id)
    stored = {
        **stored,
        "score": 9,
        "value": json.dumps(stored["value"]),
        "evidence": json.dumps(stored["evidence"]),
    }
    cols = ", ".join(stored)
    assert_db_rejects(
        engine,
        f"INSERT OR REPLACE INTO evaluation_passes ({cols}) VALUES "
        f"({', '.join(':' + c for c in stored)})",
        **stored,
    )


# --------------------------------------------------------------------------- fix 5


def _pass(packet_id: str, cycle: str | None = "c1") -> EvaluationPass:
    return EvaluationPass(
        packet_id=packet_id,
        eval_cycle_id=cycle,
        field_name="styles",
        model="fake-a",
        pass_no=1,
        role="extract",
        value=["plan"],
    )


def test_fix5_evaluation_pass_uniqueness_includes_packet(session: Session, tree) -> None:
    a, b = tree.packets[0].id, tree.packets[1].id
    make_cycle(session, a, "c1")
    session.add_all([_pass(a), _pass(b)])  # same cycle/field/pass on two packets: fine
    session.commit()
    session.add(_pass(a))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(_pass(a, cycle=None))  # the cycle id is mandatory
    with pytest.raises(IntegrityError):
        session.commit()


def test_fix5_downgrade_and_reupgrade_restore_the_recorded_schema(db_url: str) -> None:
    engine = make_engine(db_url)
    cfg = alembic_config()
    dialect = "sqlite" if db_url.startswith("sqlite") else "postgresql"
    manifest = json.loads(MANIFEST.read_text())[dialect]

    def step(fn, rev: str) -> dict:  # type: ignore[no-untyped-def]
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            fn(cfg, rev)
        with engine.connect() as connection:
            return fingerprint(connection)

    try:
        init_db(engine)
        factory = make_session_factory(engine)
        with factory() as s:
            persist_tree(s, ProjectTree.model_validate(SMALL_TREE), actor=USER)
            s.commit()
        assert step(command.downgrade, "0003") == manifest["0003"]
        assert step(command.downgrade, "0002") == manifest["0002"]
        assert step(command.upgrade, "head") == manifest[head_revision()]
        # counters are rebuilt from existing refs, so numbering continues after the round trip
        with factory() as s:
            again = persist_tree(s, ProjectTree.model_validate(SMALL_TREE), actor=USER)
            assert again.project.ref == "P-0002"
            assert again.packets[0].ref == "PP-000004"
            s.commit()
        assert step(command.downgrade, "base") == manifest["0001"]
        assert current_revision(engine) is None
        assert step(command.upgrade, "head") == manifest[head_revision()]
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- fix 6


def test_fix6_refs_are_never_reused(session: Session, tree) -> None:
    last = tree.packets[-1]
    delete_packet(session, get_packet(session, last.ref), actor=USER)
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    assert result.packets[0].ref == "PP-000004"  # PP-000003 was deleted, never reissued


def test_fix6_two_writers_get_distinct_consecutive_numbers(engine: Engine) -> None:
    factory = make_session_factory(engine)
    first = factory()
    got: dict[str, int] = {}
    errors: list[BaseException] = []

    def second_writer() -> None:
        try:
            with factory() as s:
                got["second"] = allocate(s, "test-counter")
                s.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        got["first"] = allocate(first, "test-counter")  # holds the lock until commit
        worker = threading.Thread(target=second_writer)
        worker.start()
        time.sleep(0.5)
        assert "second" not in got, "the second writer must wait for the first"
        first.commit()
        worker.join(timeout=10)
    finally:
        first.close()
    assert not errors, errors
    assert (got["first"], got["second"]) == (1, 2)


# --------------------------------------------------------------------------- fix 7


def test_fix7_unevented_orm_writes_are_rejected(session: Session, tree) -> None:
    packet = get_packet(session, tree.packets[0].ref)
    packet.title = "edited without an event"
    with pytest.raises(UnauditedWriteError):
        session.flush()
    session.rollback()

    session.add(Threshold(name="x", field_name="styles", operator=">=", value="1", action="split"))
    with pytest.raises(UnauditedWriteError):
        session.flush()
    session.rollback()

    with pytest.raises(UnauditedWriteError):
        session.execute(update(Packet).values(title="bulk"))


def test_fix7_settings_must_bump_their_version(session: Session) -> None:
    row = session.get(Setting, "aggregation.dispute_margin")
    row.value = 0.3
    record_event(
        session, actor=USER, action="setting.changed", entity_type="setting", entity_id=row.key
    )
    with pytest.raises(UnauditedWriteError, match="version"):
        session.flush()


def test_fix7_evented_writes_pass(session: Session, tree) -> None:
    packet = get_packet(session, tree.packets[0].ref)
    update_packet(session, packet, {"title": "evented"}, actor=USER)
    session.add(Threshold(name="y", field_name="styles", operator=">=", value="1", action="split"))
    record_event(session, actor=USER, action="threshold.created", entity_type="threshold")
    session.commit()


# --------------------------------------------------------------------------- fix 8


def test_fix8_supersede_with_a_new_response_structure(session: Session, tree) -> None:
    new = supersede_packet(
        session, tree.packets[0].ref, {"response_structure": "json_object"}, actor=USER
    )
    assert new.response_structure == "json_object"
    assert new.response_contract == "json_object@1.0"
    assert get_packet(session, f"{new.ref}@1").response_contract == "markdown_document@1.0"


# --------------------------------------------------------------------------- fix 9


def test_fix9_untitled_fallback() -> None:
    tree = parse_text("# P\n## O\n- [ ]\n- real task\n", fallback_name="x")
    titles = [t.title for t in tree.objectives[0].tasks]
    assert titles == [UNTITLED_TASK, "real task"]


def test_fix9_long_descriptions_are_split_not_lost(tmp_path, session: Session) -> None:  # type: ignore[no-untyped-def]
    paragraphs = [f"Paragraph {i}: " + "word " * 900 for i in range(12)]
    doc = tmp_path / "long.md"
    doc.write_text("# Big\n## Spec\n### Everything\n" + "\n\n".join(paragraphs) + "\n")
    result = ingest_file(session, doc, actor=USER)
    parts = [p for p in result.packets if p.title.startswith("Everything (part")]
    assert len(parts) >= 2
    assert all(len(p.prompt_description) <= DESCRIPTION_MAX for p in parts)
    joined = "\n\n".join(p.prompt_description.split("\n\n", 1)[1] for p in parts)
    assert all(para.strip() in joined for para in paragraphs)
    assert parts[0].title.endswith(f"(part 1/{len(parts)})")


def test_fix9_split_text_hard_cuts_only_inside_one_huge_line() -> None:
    chunks = split_text("a" * 50_000, budget=20_000)
    assert [len(c) for c in chunks] == [20_000, 20_000, 10_000]


# --------------------------------------------------------------------------- fix 10


def test_fix10_artifact_insert_accepted_without_accepted_at(engine, tree) -> None:
    now = row_of(engine, "packets", tree.packets[0].id)["created_at"]
    values = {
        "id": new_id(),
        "packet_id": tree.packets[0].id,
        "path": "x.md",
        "sha256": "0" * 64,
        "accepted": True,
        "created_at": now,
        "updated_at": now,
    }
    with pytest.raises(DBAPIError) as info:
        insert_row(engine, "artifacts", values)
    assert is_immutability_violation(info.value)
    insert_row(engine, "artifacts", {**values, "accepted": False})


# --------------------------------------------------------------------------- fix 11


def test_fix11_actor_is_typed() -> None:
    assert str(Actor.user("cli")) == "user:cli"
    assert Actor.llm("m").kind is ActorKind.LLM
    with pytest.raises(ValueError):
        Actor.user("user:cli spoof")
    with pytest.raises(TypeError):
        Actor("user", "x")  # type: ignore[arg-type]


def test_fix11_only_users_complete_or_archive_by_hand(session: Session, tree) -> None:
    for actor in (LLM, KERNEL):
        for target in (PacketStatus.COMPLETED, PacketStatus.ARCHIVED):
            with pytest.raises(InvalidTransitionError):  # archived is legacy: no entries (M3)
                transition(tree.packets[0], target, actor)
    with pytest.raises(TypeError):
        transition(tree.packets[0], PacketStatus.COMPLETED, "user:cli")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        record_event(session, actor="user:cli", action="x", entity_type="x")  # type: ignore[arg-type]
    # M2.1 fix 5: by hand, a user closes unverified work (completion needs evidence)
    with pytest.raises(InvalidTransitionError, match="not in the packet table"):
        transition(tree.packets[0], PacketStatus.COMPLETED, USER, "by hand")  # D8: no skipping
    transition(tree.packets[0], PacketStatus.CLOSED_UNVERIFIED, USER, "by hand")
    assert tree.packets[0].locked


def test_fix11_llm_cannot_skip_the_pipeline_from_any_state(session: Session, tree) -> None:
    packet = tree.packets[0]
    for status in (PacketStatus.EVALUATING_PRE, PacketStatus.READY, PacketStatus.QUEUED):
        transition(packet, status, KERNEL)
        with pytest.raises(InvalidTransitionError):
            transition(packet, PacketStatus.COMPLETED, LLM)


# --------------------------------------------------------------------------- fix 12


def test_fix12_registry_revision_recorded_and_versioned(engine, session: Session) -> None:
    first = current_registry_revision(session)
    assert first is not None and first.revision == 1 and first.field_count == 36
    assert init_db(engine)["registry_revision"] == 1  # unchanged YAML → same revision

    changed: list[CandidateFieldDefinition] = [
        d.model_copy(update={"label": "Styles (renamed)"}) if d.name == "styles" else d
        for d in load_candidate_fields()
    ]
    second = record_registry_revision(session, changed, actor=USER)
    session.commit()
    assert (second.revision, second.field_count) == (2, 36)
    assert second.sha256 != first.sha256
    assert any(d["label"] == "Styles (renamed)" for d in second.definitions)
    event = session.scalar(
        select(Event).where(Event.action == "fields.registry_revision", Event.entity_version == 2)
    )
    assert event is not None and event.after["sha256"] == second.sha256
    assert_db_rejects(
        engine, "UPDATE field_registry_revisions SET sha256 = 'x' WHERE id = :id", id=first.id
    )
    assert_db_rejects(engine, "DELETE FROM field_registry_revisions WHERE id = :id", id=first.id)
    assert session.query(FieldRegistryRevision).count() == 2
