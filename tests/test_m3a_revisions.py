"""M3a (a): in-place edits with automatic history (AC6, AC8, AC10)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from orchestrator.contracts import PacketStatus, ProjectTree
from orchestrator.db.models import ContentBlob, Event, FieldRevision, Packet, RawRevisionOutbox
from orchestrator.ingest import persist_tree
from orchestrator.repository import update_objective, update_packet, update_project
from orchestrator.revisions import drain_outbox, has_undrained, snapshot_at
from tests.conftest import SMALL_TREE, USER
from tests.dbhelpers import raw
from tests.evafixtures import drive_to


def _tree(session: Session):  # type: ignore[no-untyped-def]
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    session.commit()
    return result


@pytest.mark.parametrize("status", ["draft", "evaluating_pre", "ready", "queued"])
def test_sql_edit_of_a_developing_packet_is_drained_into_history(
    engine: Engine, session: Session, status: str
) -> None:
    """AC6: a direct SQL UPDATE succeeds in every developing status; the drain writes one
    revision per changed field with resolvable blobs, empties the outbox, and records one
    event."""
    packet = _tree(session).packets[0]
    drive_to(packet, PacketStatus(status))
    session.commit()
    raw(
        engine,
        "UPDATE packets SET prompt_description = 'CFO rewrite', repo_target_path = 'a.py' "
        "WHERE id = :id",
        id=packet.id,
    )
    assert has_undrained(session, packet.id)
    events_before = session.scalar(select(func.count()).select_from(Event))
    revisions = drain_outbox(session)
    session.commit()
    assert sorted(r.field_name for r in revisions) == ["prompt_description", "repo_target_path"]
    for r in revisions:
        assert session.get(ContentBlob, r.new_hash) is not None
        assert session.get(ContentBlob, r.old_hash) is not None
        assert r.producer == "user_sql" and r.content_revision == 1
    assert len({r.revision_group_id for r in revisions}) == 1
    assert session.get(ContentBlob, revisions[0].new_hash).body in ("CFO rewrite", "a.py")
    assert session.scalar(select(func.count()).select_from(RawRevisionOutbox)) == 0
    new_events = session.scalars(
        select(Event).where(Event.action == "packet.revised").order_by(Event.created_at)
    ).all()
    assert len(new_events) == 1 and str(new_events[0].actor) == "user:sql"
    assert session.scalar(select(func.count()).select_from(Event)) == events_before + 1
    session.refresh(packet)
    assert PacketStatus(packet.status).value == status


def test_governed_edits_keep_the_row_and_record_history(session: Session) -> None:
    """AC10: edits keep the row id; no new packet rows; the kernel producer is recorded."""
    tree = _tree(session)
    packet = tree.packets[0]
    drive_to(packet, PacketStatus.READY)
    count = session.scalar(select(func.count()).select_from(Packet))
    update_packet(session, packet, {"prompt_description": "Parse more."}, actor=USER)
    update_packet(session, packet, {"title": "Parser"}, actor=USER)
    session.commit()
    assert session.scalar(select(func.count()).select_from(Packet)) == count
    assert session.scalar(select(func.count()).where(Packet.supersedes_id.is_not(None))) == 0
    revisions = session.scalars(
        select(FieldRevision)
        .where(FieldRevision.entity_id == packet.id)
        .order_by(FieldRevision.content_revision)
    ).all()
    assert [(r.field_name, r.content_revision, r.producer) for r in revisions] == [
        ("prompt_description", 1, "user:test"),
        ("title", 2, "user:test"),
    ]
    assert packet.content_revision == 2 and PacketStatus(packet.status) is PacketStatus.READY
    # Q6 based-on: the second edit of a field is based on the first edit's value
    update_packet(session, packet, {"prompt_description": "Parse everything."}, actor=USER)
    third = session.scalars(select(FieldRevision).where(FieldRevision.content_revision == 3)).one()
    assert third.based_on_hash == revisions[0].new_hash
    # the snapshot at revision 1 is reconstructed from history (R6 OQ20)
    at1 = snapshot_at(session, packet, 1)
    assert at1["prompt_description"] == "Parse more." and at1["title"] == "Write the parser"
    assert snapshot_at(session, packet, 0)["prompt_description"] == "Parse things."


def test_project_and_objective_edit_in_place(session: Session) -> None:
    tree = _tree(session)
    update_project(session, tree.project, {"brief": "New brief."}, actor=USER)
    update_objective(session, tree.objectives[0], {"brief": "Obj brief."}, actor=USER)
    session.commit()
    kinds = session.execute(
        text("SELECT entity_type, field_name, producer FROM field_revisions ORDER BY entity_type")
    ).all()
    assert [tuple(k) for k in kinds] == [
        ("objective", "brief", "user:test"),
        ("project", "brief", "user:test"),
    ]
    assert tree.project.version == 1 and tree.project.content_revision == 1


def test_a_failed_drain_rolls_back_the_whole_edit(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6 OQ35 (PM): the drain runs in the edit's transaction, so a drain failure leaves no
    outbox row, no revision and an unchanged content_revision."""
    import orchestrator.revisions as revisions

    packet = _tree(session).packets[0]
    pid = packet.id

    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("injected drain failure")

    monkeypatch.setattr(revisions, "store_blob", boom)
    with pytest.raises(RuntimeError, match="injected"):
        update_packet(session, packet, {"prompt_description": "Never stored."}, actor=USER)
    session.rollback()
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM raw_revision_outbox")).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM field_revisions")).scalar() == 0
        row = c.execute(
            text("SELECT content_revision, prompt_description FROM packets WHERE id = :id"),
            {"id": pid},
        ).one()
    assert tuple(row) == (0, "Parse things.")


def test_gate_only_changes_do_not_bump_content_revision(session: Session) -> None:
    """R6 OQ33 (PM): status, gate revision and route changes never touch content_revision."""
    from orchestrator.contracts import Channel
    from orchestrator.gates import bump_gate

    packet = _tree(session).packets[0]
    drive_to(packet, PacketStatus.READY)
    bump_gate(session, packet, actor=USER, cause="test")
    update_packet(session, packet, {"channel": Channel.LOCAL_GENERATION}, actor=USER)
    session.commit()
    session.refresh(packet)
    assert packet.content_revision == 0
    assert session.scalar(select(func.count()).select_from(FieldRevision)) == 0
