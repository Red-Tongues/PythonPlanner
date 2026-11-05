"""Branching (B5), promotion and the approval queue; populated upgrade to 0005 (D4)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from orchestrator.branching import apply_branch_triggers, children_of, rollup_trunks
from orchestrator.cli import app
from orchestrator.confirmations import approve, list_confirmations, reject
from orchestrator.contracts import AutonomyMode, PacketStatus, Stage
from orchestrator.db import init_db, make_engine, make_session_factory
from orchestrator.db.models import BranchApplication, Event, Packet
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider
from orchestrator.repository import get_packet, set_autonomy_mode, set_setting
from tests.conftest import KERNEL, USER
from tests.evafixtures import Item, accept_all, complete_with_evidence, seed_eva

EIGHTS = {3: 8, 4: 8, 5: 8, 6: 8}
SUP = {4: (1, 0, 0), 5: (1, 0, 0)}
SPLITTABLE = "Write the parser; then the lexer and the tests"


def _splittable(session: Session, tree) -> Packet:  # type: ignore[no-untyped-def]
    packet = tree.packets[0]
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet, skip=frozenset({"task_parsability"}))
    seed_eva(session, packet, "task_parsability", {"": Item("8", "8", EIGHTS, SUP)})
    transition(packet, PacketStatus.READY, KERNEL)
    return packet


def test_split_creates_linked_children_and_keeps_the_trunk(session: Session, tree) -> None:
    set_autonomy_mode(session, Stage.BRANCHING, AutonomyMode.AUTO, actor=USER)
    trunk = _splittable(session, tree)
    provider = FakeProvider()
    outcomes = apply_branch_triggers(session, provider)
    split = [o for o in outcomes if o.action == "split"]
    assert len(split) == 1 and len(split[0].children) >= 2
    children = children_of(session, trunk)
    assert [c.ref for c in children] == split[0].children
    assert all(c.parent_id == trunk.id and c.root_id == trunk.id and c.depth == 1 for c in children)
    assert get_packet(session, trunk.ref).status is PacketStatus.WAITING_FOR_CHILDREN
    # idempotent: the rule no longer fires on a waiting trunk, and the key forbids a repeat
    assert apply_branch_triggers(session, provider) == [] or all(
        o.action != "split" for o in apply_branch_triggers(session, provider)
    )
    assert len(session.scalars(select(BranchApplication)).all()) == 1
    # roll-up: the trunk completes when every child does
    for child in children:
        complete_with_evidence(session, child, USER)
    assert rollup_trunks(session) == [trunk.ref]
    assert get_packet(session, trunk.ref).locked


def test_confirm_scope_queues_then_approve_or_reject(session: Session, tree) -> None:
    trunk = _splittable(session, tree)  # branching scope defaults to confirm
    provider = FakeProvider()
    outcome = apply_branch_triggers(session, provider)[0]
    assert outcome.action == "confirmation" and children_of(session, trunk) == []
    pending = [c for c in list_confirmations(session) if c.stage is Stage.BRANCHING]
    assert len(pending) == 1
    assert trunk.status is PacketStatus.AWAITING_CONFIRMATION  # the split awaits the owner
    result = approve(session, pending[0].id, actor=USER, provider=provider)
    assert result["children"] and children_of(session, trunk)
    # R4 MR5: the confirmed split is the approving user's move, bound to the confirmation
    split = session.scalars(
        select(Event).where(
            Event.entity_id == trunk.id,
            Event.action == "packet.transition",
            Event.reason.like("split into%"),
        )
    ).one()
    assert split.actor == str(USER) and pending[0].id in (split.reason or "")
    assert split.after is not None and split.after["status"] == "waiting_for_children"


def test_rejected_split_is_not_proposed_again(session: Session, tree) -> None:
    _splittable(session, tree)
    provider = FakeProvider()
    apply_branch_triggers(session, provider)
    pending = [c for c in list_confirmations(session) if c.stage is Stage.BRANCHING]
    reject(session, pending[0].id, actor=USER, reason="keep it whole")
    outcome = apply_branch_triggers(session, provider)[0]
    assert outcome.action == "skipped" and "rejected" in outcome.reason


def test_depth_limit(session: Session, tree) -> None:
    set_autonomy_mode(session, Stage.BRANCHING, AutonomyMode.AUTO, actor=USER)
    set_setting(session, "branching.max_depth", 0, actor=USER)
    _splittable(session, tree)
    outcome = apply_branch_triggers(session, FakeProvider())[0]
    assert outcome.action == "skipped" and "max_branch_depth" in outcome.reason


def test_legacy_rows_stay_valid_and_only_reenter_evaluation(db_url: str) -> None:
    """Deviation P3 (and D4's intent): M1-era statuses and legacy evaluation cycles remain valid
    rows; a legacy packet can only re-enter the pipeline at evaluating_pre, never resume."""
    from orchestrator.db.ddl import is_transition_violation

    engine = make_engine(db_url)
    try:
        init_db(engine)
        now = "2026-11-03 12:00:00"
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO projects (id, ref, name, brief, sectors, core_values, goals,"
                    " reference_list, source, version, locked, created_at, updated_at) "
                    "VALUES ('p1', 'P-0001', 'Legacy', '', '[]', '[]', '[]', '[]', 'm', 1, "
                    ":f, :t, :t)"
                ),
                {"t": now, "f": False},
            )
            for pid, status in (("k1", "in_progress"), ("k2", "processed")):
                c.execute(
                    text(
                        "INSERT INTO packets (id, ref, project_id, position, packet_kind, title,"
                        " prompt_description, response_structure, response_contract, "
                        "manual_boost, status, evaluations_count, locked, version, depth, "
                        "source, created_at, updated_at) VALUES (:id, :ref, 'p1', 0, 'task', "
                        "'Legacy', 'x', 'markdown_document', 'markdown_document@1.0', 0, :s, 0, "
                        ":f, 1, 0, 'm', :t, :t)"
                    ),
                    {"id": pid, "ref": f"PP-00000{pid[1]}", "s": status, "t": now, "f": False},
                )
            c.execute(
                text(
                    "INSERT INTO evaluation_cycles (id, packet_id, packet_version, phase,"
                    " target_hash, pass_plan, models, state, created_at, updated_at) VALUES"
                    " ('legacy-cycle', 'k1', 1, 'pre', 'legacy', 'legacy', '{}', 'legacy', :t, :t)"
                ),
                {"t": now},
            )
        for target in ("running", "evaluating_post", "completed"):
            with pytest.raises(Exception) as info, engine.begin() as c:
                c.execute(text("UPDATE packets SET status = :s WHERE id = 'k1'"), {"s": target})
            assert is_transition_violation(info.value) or "locked" in str(info.value)
        with engine.begin() as c:
            for pid in ("k1", "k2"):
                c.execute(
                    text("UPDATE packets SET status = 'evaluating_pre' WHERE id = :id"),
                    {"id": pid},
                )
        with engine.connect() as c:
            cycle = c.execute(
                text("SELECT state, packet_id FROM evaluation_cycles WHERE id = 'legacy-cycle'")
            ).one()
            assert tuple(cycle) == ("legacy", "k1")
            kind = c.execute(text("SELECT packet_kind, tags FROM packets WHERE id = 'k1'")).one()
            assert (json.loads(kind.tags) if isinstance(kind.tags, str) else kind.tags) == {}
    finally:
        engine.dispose()


def test_cli_acceptance_chain(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """M2 acceptance criteria 1-4 through the CLI (FakeProvider by default)."""
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    run = CliRunner()

    def cli(*args: str):  # type: ignore[no-untyped-def]
        result = run.invoke(app, ["--database-url", url, *args])
        assert result.exit_code == 0, result.output
        return result.output

    cli("init-db")
    cli("ingest", "examples/sample_project_plan.md")
    out = cli("evaluate")
    assert out.count("→ ready") == 15
    shown = cli("consensus", "PP-000001")
    assert "accepted" in shown and "conf 0.80" in shown and "SUPPORT:" in shown
    out = cli("branch", "apply")
    assert "confirmation" in out
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        pending = [c for c in list_confirmations(s) if c.stage is Stage.BRANCHING]
    engine.dispose()
    out = cli("confirmations", "approve", pending[0].id[:8])
    assert "children" in out
    queue_lines = [line for line in cli("queue").splitlines() if line.strip()]
    positions = [int(line.split()[0]) for line in queue_lines]
    assert positions == list(range(1, len(positions) + 1)) and positions
    assert "PP-" in cli("stats")
