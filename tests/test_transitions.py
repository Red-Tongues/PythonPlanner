"""C23: the packet and run transition tables (``orchestrator.transitions``).

* Every (from, to) pair *not* in a table is rejected, for every actor kind — exhaustively, at the
  application layer and (``test_db_*``) by the migration 0007 triggers on both dialects.
* D8 §4's forbidden transitions fail.
* ``docs/ARCHITECTURE.md`` §7 is the rendered table (``scripts/transition_tables.py``).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from orchestrator.actors import Actor, ActorKind
from orchestrator.contracts import GenerationRunState, PacketStatus
from orchestrator.errors import InvalidTransitionError
from orchestrator.transitions import (
    PACKET_TRANSITIONS,
    RUN_TRANSITIONS,
    TERMINAL,
    check_packet,
    check_run,
    packet_pairs,
    run_pairs,
)

S, R = PacketStatus, GenerationRunState
ACTORS = {
    ActorKind.USER: Actor.user("t"),
    ActorKind.SYSTEM: Actor.system("t"),
    ActorKind.LLM: Actor.llm("t"),
}
ROOT = Path(__file__).resolve().parents[1]


def test_every_packet_pair_not_in_the_table_is_rejected_for_every_actor() -> None:
    checked = 0
    for src, dst in itertools.product(PacketStatus, PacketStatus):
        rule = PACKET_TRANSITIONS.get((src.value, dst.value))
        for kind, actor in ACTORS.items():
            if rule is not None and kind in rule.actors:
                assert check_packet(src, dst, actor) is rule
            else:
                with pytest.raises(InvalidTransitionError):
                    check_packet(src, dst, actor)
                checked += 1
    assert checked > 700  # 18 × 18 × 3 minus the allowed moves


def test_every_run_pair_not_in_the_table_is_rejected_for_every_actor() -> None:
    for src, dst in itertools.product(GenerationRunState, GenerationRunState):
        rule = RUN_TRANSITIONS.get((src.value, dst.value))
        for kind, actor in ACTORS.items():
            if rule is not None and kind in rule.actors:
                assert check_run(src, dst, actor) is rule
            else:
                with pytest.raises(InvalidTransitionError):
                    check_run(src, dst, actor)


def test_llm_actors_never_move_anything() -> None:
    assert all(ActorKind.LLM not in r.actors for r in PACKET_TRANSITIONS.values())
    assert all(ActorKind.LLM not in r.actors for r in RUN_TRANSITIONS.values())


def test_terminal_statuses_have_no_exits_and_archived_no_entries() -> None:
    assert not [k for k in PACKET_TRANSITIONS if S(k[0]) in TERMINAL]
    assert not [k for k in PACKET_TRANSITIONS if k[1] == S.ARCHIVED.value]
    for state in (R.FAILED, R.STALE, R.SUCCEEDED, R.ABANDONED):
        assert not [k for k in RUN_TRANSITIONS if k[0] == state.value]


def test_every_status_is_reachable_or_legacy() -> None:
    reached = {t for _, t in PACKET_TRANSITIONS} | {S.DRAFT.value}
    legacy = {S.ARCHIVED, S.EVALUATING, S.IN_PROGRESS, S.PROCESSED}
    assert {s.value for s in PacketStatus} - reached == {s.value for s in legacy}


# --------------------------------------------------------------------------- D8 §4

USER, KERNEL = ACTORS[ActorKind.USER], ACTORS[ActorKind.SYSTEM]


@pytest.mark.parametrize("target", list(PacketStatus))
@pytest.mark.parametrize("terminal", [S.COMPLETED, S.CLOSED_UNVERIFIED])
def test_d8_1_2_terminal_packets_never_move(terminal: PacketStatus, target: PacketStatus) -> None:
    for actor in ACTORS.values():
        with pytest.raises(InvalidTransitionError):
            check_packet(terminal, target, actor)


def test_d8_3_running_never_skips_evaluating_post() -> None:
    for actor in ACTORS.values():
        with pytest.raises(InvalidTransitionError):
            check_packet(S.RUNNING, S.COMPLETED, actor)


def test_d8_6_adding_a_hold_never_moves_blocked_to_held_for_the_kernel() -> None:
    # blocked → held exists only as the owner revoking the last BLOCK (HOLD remains)
    with pytest.raises(InvalidTransitionError):
        check_packet(S.BLOCKED, S.HELD, KERNEL)


def test_d8_7_held_never_returns_to_ready_or_queued_without_the_owner() -> None:
    for target in (S.READY, S.QUEUED):
        for actor in ACTORS.values():
            with pytest.raises(InvalidTransitionError):
                check_packet(S.HELD, target, actor)
    with pytest.raises(InvalidTransitionError):
        check_packet(S.HELD, S.EVALUATING_PRE, KERNEL)  # never automatic (C33)
    check_packet(S.HELD, S.EVALUATING_PRE, USER)


def test_needs_review_never_loops_back_on_its_own() -> None:
    with pytest.raises(InvalidTransitionError):
        check_packet(S.NEEDS_REVIEW, S.EVALUATING_PRE, KERNEL)


def test_kernel_cannot_approve_on_the_owners_behalf() -> None:
    for target in (S.RUNNING, S.QUEUED, S.EVALUATING_POST, S.COMPLETED):
        with pytest.raises(InvalidTransitionError):
            check_packet(S.AWAITING_CONFIRMATION, target, KERNEL)
    with pytest.raises(InvalidTransitionError):
        check_run(R.AWAITING_CONFIRMATION, R.DISPATCHED, KERNEL)
    with pytest.raises(InvalidTransitionError):
        check_run(R.AWAITING_CONFIRMATION, R.FAILED, KERNEL)  # only explicit cancellation (E2)


def test_runs_never_skip_validation_or_revive() -> None:
    for src in (R.PLANNED, R.DISPATCHED, R.AWAITING_RESULT):
        with pytest.raises(InvalidTransitionError):
            check_run(src, R.ACCEPTED, KERNEL)
    for src in (R.STALE, R.FAILED):
        for dst in GenerationRunState:
            with pytest.raises(InvalidTransitionError):
                check_run(src, dst, KERNEL)
    with pytest.raises(InvalidTransitionError):
        check_run(R.CLAIMED, R.DISPATCHED, KERNEL)  # a legacy claim has no fencing token


def test_errata_rows_are_present() -> None:
    assert ActorKind.USER in PACKET_TRANSITIONS[("awaiting_confirmation", "evaluating_pre")].actors
    assert ("dispatched", "dispatched") in RUN_TRANSITIONS  # E11 deadline retry
    assert "E7" in PACKET_TRANSITIONS[("running", "evaluating_pre")].guard
    assert "E8" in RUN_TRANSITIONS[("accepted", "stale")].trigger


# --------------------------------------------------------------------------- docs


def test_architecture_doc_publishes_the_tables() -> None:
    from scripts.transition_tables import render

    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert render() in doc, "regenerate with: python scripts/transition_tables.py"


def test_pair_lists_are_sorted_and_complete() -> None:
    assert packet_pairs() == sorted(PACKET_TRANSITIONS)
    assert set(run_pairs()) == {k for k in RUN_TRANSITIONS if k[0] != k[1]}


def test_d9_r1_branch_kill_blocks_and_never_closes() -> None:
    close = PACKET_TRANSITIONS[("ready", "closed_unverified")]
    assert "branch kill is a BLOCK" in close.trigger
    for src in ("draft", "evaluating_pre", "ready", "queued", "needs_review", "failed", "held"):
        assert "branch kill" in PACKET_TRANSITIONS[(src, "blocked")].trigger
    check_packet(S.BLOCKED, S.EVALUATING_PRE, USER)  # undone by revoking the BLOCK
