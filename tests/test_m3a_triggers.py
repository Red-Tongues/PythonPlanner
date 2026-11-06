"""M3a (a): revision capture and append-only history, at the database (both dialects).

T1-T6 are the trigger proof of T1-C v2 §8, re-run against the production DDL (``db/ddl.py``):
``content_revision`` plays the role of the proof's ``version`` (R6 OQ33). R6 OQ32: guards are
BEFORE triggers and the outbox is an AFTER trigger, so a rejected change writes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text

from orchestrator.contracts import ProjectTree
from orchestrator.ingest import persist_tree
from tests.conftest import SMALL_TREE, USER
from tests.dbhelpers import assert_db_rejects, raw


def _packet(session) -> str:  # type: ignore[no-untyped-def]
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    session.commit()
    return result.packets[0].id


def _state(engine: Engine, pid: str) -> dict[str, Any]:
    with engine.connect() as c:
        row = c.execute(
            text("SELECT content_revision, last_producer FROM packets WHERE id = :id"), {"id": pid}
        ).one()
        outbox = c.execute(
            text(
                "SELECT field_name, old_value, new_value, producer, tx_id, content_revision "
                "FROM raw_revision_outbox WHERE entity_id = :id ORDER BY id"
            ),
            {"id": pid},
        ).all()
    return {"rev": row[0], "producer": row[1], "outbox": outbox}


def test_t1_to_t6_trigger_proof(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    pid = _packet(session)
    start = _state(engine, pid)
    assert start["rev"] == 0 and start["outbox"] == []

    # T1: a no-op assignment logs nothing and does not bump
    raw(engine, "UPDATE packets SET title = title, prompt_description = prompt_description "
        "WHERE id = :id", id=pid)  # fmt: skip
    assert _state(engine, pid) == start

    # T2: a CAS / bookkeeping-only update logs nothing
    raw(engine, "UPDATE packets SET fencing_token = fencing_token + 1, gate_revision = "
        "gate_revision + 1 WHERE id = :id", id=pid)  # fmt: skip
    assert _state(engine, pid) == start

    # T3: a two-field edit including NULL → +1 once, two rows sharing one tx id
    raw(engine, "UPDATE packets SET repo_target_path = NULL, prompt_description = 'Edited.', "
        "title = title WHERE id = :id", id=pid)  # fmt: skip
    raw(engine, "UPDATE packets SET repo_target_path = 'src/x.py' WHERE id = :id", id=pid)
    s = _state(engine, pid)
    assert s["rev"] == 2 and s["producer"] == "user_sql"
    first = [r for r in s["outbox"] if r.content_revision == 1]
    assert {r.field_name for r in first} == {"prompt_description"}
    assert {r.producer for r in s["outbox"]} == {"user_sql"}

    raw(engine, "UPDATE packets SET title = 'New title', manual_boost = 3 WHERE id = :id", id=pid)
    s = _state(engine, pid)
    pair = [r for r in s["outbox"] if r.content_revision == 3]
    assert s["rev"] == 3 and {r.field_name for r in pair} == {"title", "manual_boost"}
    assert len({r.tx_id for r in pair}) == 1

    # T4: a kernel CAS that bumps the revision itself is +1, not +2
    raw(engine, "UPDATE packets SET title = 'Kernel title', last_producer = 'kernel:x', "
        "content_revision = content_revision + 1 WHERE id = :id AND content_revision = 3",
        id=pid)  # fmt: skip
    s = _state(engine, pid)
    assert s["rev"] == 4 and s["outbox"][-1].producer == "kernel:x"
    assert s["outbox"][-1].content_revision == 4

    # T5: a user_sql edit after a kernel edit is attributed to user_sql
    raw(engine, "UPDATE packets SET title = 'CFO title' WHERE id = :id", id=pid)
    s = _state(engine, pid)
    assert s["rev"] == 5 and s["outbox"][-1].producer == "user_sql"

    # T6: setting content_revision to itself explicitly still bumps
    raw(engine, "UPDATE packets SET title = 'Again', content_revision = content_revision "
        "WHERE id = :id", id=pid)  # fmt: skip
    s = _state(engine, pid)
    assert s["rev"] == 6 and s["outbox"][-1].old_value == "CFO title"


def test_status_change_is_not_a_content_revision(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    """R6 OQ33: status belongs to gate_revision, never to the outbox path."""
    pid = _packet(session)
    raw(engine, "UPDATE packets SET status = 'evaluating_pre' WHERE id = :id", id=pid)
    assert _state(engine, pid)["rev"] == 0 and _state(engine, pid)["outbox"] == []


def test_rejected_transition_writes_no_outbox_rows(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    """R6 OQ32: the transition guard (BEFORE) rejects before the outbox (AFTER) runs."""
    pid = _packet(session)
    assert_db_rejects(
        engine,
        "UPDATE packets SET status = 'running', title = 'sneaky' WHERE id = :id",
        either=True,
        id=pid,
    )
    s = _state(engine, pid)
    assert s["rev"] == 0 and s["outbox"] == []


def test_locked_packet_edit_writes_no_outbox_rows(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    pid = _packet(session)
    raw(engine, "UPDATE packets SET status = 'closed_unverified', locked = :t, completed_at = "
        "CURRENT_TIMESTAMP WHERE id = :id", id=pid, t=True)  # fmt: skip
    assert_db_rejects(engine, "UPDATE packets SET title = 'x' WHERE id = :id", id=pid)
    s = _state(engine, pid)
    assert s["rev"] == 0 and s["outbox"] == []


def test_project_and_objective_edits_are_captured(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    session.commit()
    project, objective = result.project.id, result.objectives[0].id
    raw(engine, "UPDATE projects SET brief = 'New brief' WHERE id = :id", id=project)
    raw(engine, "UPDATE objectives SET brief = 'Obj brief' WHERE id = :id", id=objective)
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT entity_type, field_name FROM raw_revision_outbox ORDER BY id")
        ).all()
        revs = c.execute(
            text(
                "SELECT (SELECT content_revision FROM projects WHERE id = :p), "
                "(SELECT content_revision FROM objectives WHERE id = :o)"
            ),
            {"p": project, "o": objective},
        ).one()
    assert [tuple(r) for r in rows] == [("project", "brief"), ("objective", "brief")]
    assert tuple(revs) == (1, 1)


APPEND_ONLY = {
    "content_blobs": (
        "hash",
        "INSERT INTO content_blobs (hash, body, created_at) VALUES ('h1', 'x', CURRENT_TIMESTAMP)",
        "body = 'y'",
    ),  # fmt: skip
    "feedback_events": (
        "id",
        "INSERT INTO feedback_events (id, packet_id, actor, kind, reason, created_at) VALUES "
        "('h1', :pid, 'user:x', 'boost', 'r', CURRENT_TIMESTAMP)",
        "reason = 'y'",
    ),
}


@pytest.mark.parametrize("table", sorted(APPEND_ONLY))
def test_new_history_tables_are_append_only(engine: Engine, session, table: str) -> None:  # type: ignore[no-untyped-def]
    pid = _packet(session)
    key, insert, change = APPEND_ONLY[table]
    raw(engine, insert, pid=pid)
    assert_db_rejects(engine, f"UPDATE {table} SET {change} WHERE {key} = 'h1'")
    assert_db_rejects(engine, f"DELETE FROM {table} WHERE {key} = 'h1'")


def test_field_revisions_and_sealed_bundles_are_append_only(engine: Engine, session) -> None:  # type: ignore[no-untyped-def]
    """AC7 for field_revisions and sealed_bundles."""
    pid = _packet(session)
    raw(engine, "INSERT INTO content_blobs (hash, body, created_at) VALUES ('b', 'x', "
        "CURRENT_TIMESTAMP)")  # fmt: skip
    raw(
        engine,
        "INSERT INTO field_revisions (id, entity_type, entity_id, field_name, old_hash, new_hash,"
        " producer, content_revision, revision_group_id, manifest_hash, created_at) VALUES "
        "('fr', 'packet', :pid, 'title', 'b', 'b', 'user_sql', 1, 'g', 'm', CURRENT_TIMESTAMP)",
        pid=pid,
    )
    raw(
        engine,
        "INSERT INTO sealed_bundles (id, packet_id, claim_content_revision, snapshot_hash, "
        "bundle_hash, composite_grade_e4, created_at) VALUES ('sb', :pid, 0, 'b', 'h', 5000, "
        "CURRENT_TIMESTAMP)",
        pid=pid,
    )
    for table, key in (("field_revisions", "fr"), ("sealed_bundles", "sb")):
        assert_db_rejects(engine, f"UPDATE {table} SET created_at = created_at WHERE id = '{key}'")
        assert_db_rejects(engine, f"DELETE FROM {table} WHERE id = '{key}'")
    with pytest.raises(Exception):  # noqa: B017 - UNIQUE(packet_id): a genPacket seals once
        raw(
            engine,
            "INSERT INTO sealed_bundles (id, packet_id, claim_content_revision, snapshot_hash, "
            "bundle_hash, composite_grade_e4, created_at) VALUES ('sb2', :pid, 0, 'b', 'h', 1, "
            "CURRENT_TIMESTAMP)",
            pid=pid,
        )
