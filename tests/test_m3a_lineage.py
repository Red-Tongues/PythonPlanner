"""Argon K1 (amends R6 OQ13/OQ24): ``lineage_key`` separates re-imagination lineages from the
split (federation) tree. Edges, their satisfaction, centrality and the lineage part of the
lockout use ``lineage_key``; ``root_id`` stays the split tree. Both dialects."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestrator.contracts import ProjectTree
from orchestrator.db.models import Packet, new_id
from orchestrator.db.refs import next_packet_ref
from orchestrator.evaluation.readiness import unmet_dependencies
from orchestrator.events import record_event
from orchestrator.governance import add_dependency
from orchestrator.ingest import persist_tree
from orchestrator.queue import _edges, lockout_set, scored_rows
from tests.conftest import USER
from tests.evafixtures import FIXTURE, complete_with_evidence

TREE = {
    "project": {"name": "L"},
    "objectives": [
        {
            "name": "O",
            "tasks": [{"title": f"T{i}", "prompt_description": f"Do {i}."} for i in range(4)],
        }
    ],
}


def _child(session: Session, parent: Packet, *, reimagine: bool, position: int = 0) -> Packet:
    """A split child (own lineage, ``root_id`` = the split tree) or a re-imagined child
    (inherits ``lineage_key`` and ``root_id``, ``lineage_depth + 1``), built as the kernel does."""
    child = Packet(
        id=new_id(),
        ref=next_packet_ref(session),
        project_id=parent.project_id,
        objective_id=parent.objective_id,
        position=parent.position if reimagine else parent.position * 100 + position,
        title=f"child of {parent.ref}",
        prompt_description="Child work.",
        parent_id=parent.id,
        root_id=parent.root_id or parent.id,
        depth=parent.depth if reimagine else parent.depth + 1,
        lineage_depth=parent.lineage_depth + 1 if reimagine else 0,
        lineage_key=parent.lineage_key if reimagine else None,  # None → its own id
        source="gen_eva" if reimagine else "branch",
    )
    session.add(child)
    record_event(session, actor=FIXTURE, action="t", entity_type="packet", entity_id=child.id)
    session.flush()
    return child


def _centrality(session: Session, packet: Packet) -> int:
    return int(
        session.execute(
            text(
                "SELECT direct_dependents FROM v_packet_relational_centrality WHERE packet_id = :id"
            ),
            {"id": packet.id},
        ).scalar()
    )


def test_split_siblings_are_separate_lineages(session: Session) -> None:
    trunk, x, y, z = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets
    a = _child(session, trunk, reimagine=False, position=1)
    b = _child(session, trunk, reimagine=False, position=2)
    assert (a.lineage_key, b.lineage_key) == (a.id, b.id)
    assert a.root_id == b.root_id == trunk.id and a.lineage_depth == b.lineage_depth == 0
    add_dependency(session, x.ref, a.ref, actor=USER)
    add_dependency(session, y.ref, b.ref, actor=USER)
    add_dependency(session, z.ref, trunk.ref, actor=USER)
    assert _centrality(session, a) == 1 and _centrality(session, b) == 1
    complete_with_evidence(session, a, USER, grade_e4=7000)
    assert unmet_dependencies(session, x) == ()
    assert unmet_dependencies(session, y) == (b.ref,)  # a sealed sibling does not satisfy it
    assert unmet_dependencies(session, z) == (trunk.ref,)  # nor the split parent's edge
    scored = {r["packet_id"]: r for r in scored_rows(session)}
    locked = lockout_set({b.id}, scored, _edges(session))
    assert a.id not in locked and b.id in locked  # locking B never locks sibling A


def test_mixed_ancestry_split_reimagine_split(session: Session) -> None:
    trunk, x, y, _ = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets
    a1 = _child(session, trunk, reimagine=False, position=1)
    complete_with_evidence(session, a1, USER, grade_e4=5000)
    a2 = _child(session, a1, reimagine=True)
    c1 = _child(session, a2, reimagine=False, position=1)
    # inherited vs fresh keys
    assert a2.lineage_key == a1.id and a2.lineage_depth == 1 and a2.root_id == trunk.id
    assert c1.lineage_key == c1.id and c1.lineage_depth == 0 and c1.root_id == trunk.id
    # an edge to any member resolves to the lineage
    edge_x = add_dependency(session, x.ref, a2.ref, actor=USER)
    edge_y = add_dependency(session, y.ref, c1.ref, actor=USER)
    assert edge_x.depends_on_id == a1.id and edge_y.depends_on_id == c1.id
    assert unmet_dependencies(session, x) == (a1.ref,)  # a1 sealed at 5000 < 7000
    complete_with_evidence(session, a2, USER, grade_e4=7000)
    assert unmet_dependencies(session, x) == ()  # a later lineage member satisfies it
    assert unmet_dependencies(session, y) == (c1.ref,)  # the split grandchild is its own
