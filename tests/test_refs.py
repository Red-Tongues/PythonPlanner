"""Atomic ref allocation (R1 fix 6): refs are never reused; concurrent writers get distinct
numbers.
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from orchestrator.contracts import ProjectTree
from orchestrator.db import make_session_factory
from orchestrator.db.refs import allocate
from orchestrator.ingest import persist_tree
from orchestrator.repository import (
    delete_packet,
    get_packet,
)
from tests.conftest import SMALL_TREE, USER

# --------------------------------------------------------------------------- ref counters


def test_refs_are_never_reused(session: Session, tree) -> None:
    last = tree.packets[-1]
    delete_packet(session, get_packet(session, last.ref), actor=USER)
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    assert result.packets[0].ref == "PP-000004"  # PP-000003 was deleted, never reissued


def test_two_writers_get_distinct_consecutive_numbers(engine: Engine) -> None:
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
