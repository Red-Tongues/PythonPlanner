"""A2: supersede-by-new-version for packets, objectives and projects; lineage-head views."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.contracts import PacketStatus
from orchestrator.db.models import (
    Event,
    Packet,
    Setting,
    new_id,
)
from orchestrator.errors import (
    AlreadySupersededError,
    ImmutableRecordError,
    NotFoundError,
)
from orchestrator.lifecycle import transition
from orchestrator.repository import (
    delete_packet,
    get_objective,
    get_packet,
    get_project,
    objectives_of,
    packets_of,
    set_setting,
    update_packet,
)
from tests.conftest import KERNEL, USER
from tests.dbhelpers import (
    assert_db_rejects,
    assert_db_rejects_insert,
    insert_row,
    row_of,
)
from tests.evafixtures import complete_with_evidence
from tests.legacy import supersede_objective, supersede_packet, supersede_project


def _row(session: Session, packet_id: str) -> dict:
    with session.bind.connect() as connection:  # type: ignore[union-attr]
        result = connection.execute(text("SELECT * FROM packets WHERE id = :id"), {"id": packet_id})
        return dict(result.mappings().one())


def test_supersede_locked_packet_creates_new_version(session: Session, tree) -> None:
    old = tree.packets[0]
    complete_with_evidence(session, old, USER)
    session.commit()
    before = _row(session, old.id)

    new = supersede_packet(
        session,
        old.ref,
        {"title": "Write the parser, properly"},
        actor=USER,
        reason="correction",
    )
    session.commit()

    assert _row(session, old.id) == before  # the old row is untouched
    assert (new.ref, new.version, new.supersedes_id) == (old.ref, 2, old.id)
    assert new.status is PacketStatus.DRAFT and not new.locked
    assert (new.parent_id, new.root_id, new.objective_id) == (
        old.parent_id,
        old.root_id,
        old.objective_id,
    )
    assert new.title == "Write the parser, properly"
    assert new.prompt_description == old.prompt_description

    assert get_packet(session, old.ref).id == new.id  # bare ref → head
    assert get_packet(session, f"{old.ref}@1").id == old.id
    assert [p.version for p in packets_of(session, tree.objectives[0])][:1] == [2]

    event = session.scalar(select(Event).where(Event.action == "packet.superseded"))
    assert event.entity_version == 2 and event.before["version"] == 1


def test_history_is_linear_and_never_edited(session: Session, tree) -> None:
    ref = tree.packets[1].ref
    supersede_packet(session, ref, {"prompt_description": "v2"}, actor=USER)
    with pytest.raises(AlreadySupersededError):
        supersede_packet(session, f"{ref}@1", {}, actor=USER)
    with pytest.raises(ImmutableRecordError, match="superseded"):
        update_packet(session, get_packet(session, f"{ref}@1"), {"title": "x"}, actor=USER)
    with pytest.raises(ImmutableRecordError):
        transition(get_packet(session, f"{ref}@1"), PacketStatus.QUEUED, KERNEL)
    v3 = supersede_packet(session, ref, {"prompt_description": "v3"}, actor=USER)
    assert v3.version == 3


def test_supersede_validates_changes(session: Session, tree) -> None:
    with pytest.raises(ValueError, match="cannot change"):
        supersede_packet(session, tree.packets[0].ref, {"status": "completed"}, actor=USER)
    with pytest.raises(Exception, match="unknown response structure"):
        supersede_packet(session, tree.packets[0].ref, {"response_structure": "haiku"}, actor=USER)
    with pytest.raises(NotFoundError):
        supersede_packet(session, "PP-424242", {}, actor=USER)


def test_packet_heads_view_follows_supersedes_chain(session: Session, tree) -> None:
    ref = tree.packets[2].ref
    v2 = supersede_packet(session, ref, {"title": "Release v2"}, actor=USER)
    v3 = supersede_packet(session, ref, {"title": "Release v3"}, actor=USER)
    session.commit()
    rows = session.execute(
        text("SELECT id, head_id, head_version, is_head FROM v_packet_heads WHERE head_ref = :r"),
        {"r": ref},
    ).all()
    assert {r.id for r in rows} == {tree.packets[2].id, v2.id, v3.id}
    assert {r.head_id for r in rows} == {v3.id}
    assert {r.head_version for r in rows} == {3}
    assert [bool(r.is_head) for r in rows if r.id == v3.id] == [True]
    total = session.scalar(text("SELECT count(*) FROM v_packet_heads"))
    assert total == session.query(Packet).count()


def test_supersede_project_and_objective_keep_children(session: Session, tree) -> None:
    project = supersede_project(session, tree.project.ref, {"brief": "Sharper brief"}, actor=USER)
    objective = supersede_objective(
        session, tree.objectives[0].ref, {"name": "Build it"}, actor=USER
    )
    session.commit()
    assert get_project(session, tree.project.ref).id == project.id
    assert project.version == 2 and project.supersedes_id == tree.project.id
    # children are not re-pointed; they belong to the lineage
    assert objective.project_id == tree.project.id
    assert [o.name for o in objectives_of(session, project)] == ["Build it", "Ship"]
    assert get_objective(session, tree.objectives[0].ref).version == 2
    assert len(packets_of(session, objective)) == 2
    assert (
        session.scalar(
            text("SELECT head_version FROM v_project_heads WHERE id = :id"), {"id": tree.project.id}
        )
        == 2
    )
    with pytest.raises(ValueError):
        supersede_project(session, tree.project.ref, {"ref": "P-9999"}, actor=USER)


def test_settings_carry_a_version(session: Session) -> None:
    row = set_setting(session, "aggregation.dispute_margin", 0.2, actor=USER)
    assert (row.value, row.version) == (0.2, 2)
    assert session.get(Setting, "aggregation.cross_weight").version == 1
    with pytest.raises(NotFoundError):
        set_setting(session, "nope", 1, actor=USER)


# ------------------------------------------------- lineage invariants and supersede (R1 fixes 2, 8)


def test_successor_must_keep_ref_version_and_ancestry(engine, tree) -> None:
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


@pytest.mark.parametrize(
    ("column", "value"),
    [("ref", "PP-777777"), ("version", 5), ("parent_id", None), ("root_id", None)],
)
def test_lineage_fields_never_change(engine, tree, column, value) -> None:
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


def test_superseding_versions_and_rows_with_dependents_cannot_be_deleted(
    engine, session: Session, tree
) -> None:
    v2 = supersede_packet(session, tree.packets[2].ref, {"title": "v2"}, actor=USER)
    session.commit()
    assert_db_rejects(engine, "DELETE FROM packets WHERE id = :id", id=v2.id)
    with pytest.raises(ImmutableRecordError, match="archive instead"):
        delete_packet(session, get_packet(session, v2.ref), actor=USER)

    # a child packet makes its parent undeletable (DB and repository)
    parent = tree.packets[0]
    child_id = new_id()
    child = {
        **row_of(engine, "packets", parent.id),
        "id": child_id,
        "lineage_key": child_id,  # a split child starts its own lineage (Argon K1)
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


def test_a_plain_v1_draft_can_still_be_deleted(session: Session, tree) -> None:
    delete_packet(session, get_packet(session, tree.packets[1].ref), actor=USER)
    session.commit()


def test_supersede_with_a_new_response_structure(session: Session, tree) -> None:
    new = supersede_packet(
        session, tree.packets[0].ref, {"response_structure": "json_object"}, actor=USER
    )
    assert new.response_structure == "json_object"
    assert new.response_contract == "json_object@1.0"
    assert get_packet(session, f"{new.ref}@1").response_contract == "markdown_document@1.0"
