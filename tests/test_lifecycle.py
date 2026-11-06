from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import PacketStatus
from orchestrator.db.models import Event
from orchestrator.errors import ImmutableRecordError, InvalidTransitionError
from orchestrator.lifecycle import ALLOWED_TRANSITIONS, can_transition, transition
from tests.conftest import KERNEL, USER
from tests.evafixtures import complete_with_evidence

S = PacketStatus
# B9 lifecycle up to evaluating_post; completing from there needs post-phase consensus (see
# tests/test_readiness.py), so the happy path here ends with a user completing by hand.
HAPPY_PATH = [
    S.EVALUATING_PRE,
    S.READY,
    S.QUEUED,
    S.RUNNING,
    S.EVALUATING_POST,
]


def _transition_events(session: Session, packet_id: str) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.entity_id == packet_id, Event.action == "packet.transition")
        .order_by(Event.created_at)
    )
    return list(session.scalars(stmt))


def test_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(PacketStatus)
    assert ALLOWED_TRANSITIONS[S.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[S.ARCHIVED] == frozenset()


def test_happy_path_writes_one_event_per_transition(session: Session, tree) -> None:
    packet = tree.packets[0]
    for status in HAPPY_PATH:
        transition(packet, status, KERNEL, f"to {status}")
    session.commit()
    events = _transition_events(session, packet.id)
    assert [e.after["status"] for e in events] == [s.value for s in HAPPY_PATH]
    assert events[0].before["status"] == "draft"
    assert all(e.actor == str(KERNEL) for e in events)
    # M3a (R6): evaluating_post → completed is retired for genPackets; they seal after gen_eva
    with pytest.raises(InvalidTransitionError, match="not in the packet table"):
        transition(packet, S.COMPLETED, KERNEL)
    with pytest.raises(InvalidTransitionError, match="not in the packet table"):
        transition(packet, S.COMPLETED, USER, "accepted by hand")
    complete_with_evidence(session, packet, KERNEL)  # M3a: a genPacket finishes sealed
    assert packet.locked and packet.completed_at is not None
    assert packet.status is S.SEALED


def test_illegal_transition_raises_and_writes_nothing(session: Session, tree) -> None:
    packet = tree.packets[0]
    count = session.scalar(select(func.count()).select_from(Event))
    with pytest.raises(InvalidTransitionError, match="draft → running"):
        transition(packet, S.RUNNING, KERNEL)
    with pytest.raises(InvalidTransitionError):
        transition(packet, S.COMPLETED, KERNEL)  # the kernel cannot skip the pipeline
    assert session.scalar(select(func.count()).select_from(Event)) == count
    assert packet.status is S.DRAFT


def test_user_closes_unverified_work_but_completion_needs_evidence(session: Session, tree) -> None:
    """M2.1 fix 5, as tightened by D8/C23: completion only from evaluating_post (or roll-up /
    an optional-scan approval), and closing unverified needs a reason."""
    user = Actor.user("cli")
    assert not can_transition(S.DRAFT, S.COMPLETED, user)  # D8: no skipping the pipeline
    with pytest.raises(InvalidTransitionError, match="not in the packet table"):
        transition(tree.packets[0], S.COMPLETED, user, "done by hand")
    with pytest.raises(InvalidTransitionError, match="needs a reason"):
        transition(tree.packets[0], S.CLOSED_UNVERIFIED, user)
    transition(tree.packets[0], S.CLOSED_UNVERIFIED, user, "done by hand")
    assert tree.packets[0].locked and tree.packets[0].status is S.CLOSED_UNVERIFIED
    assert not can_transition(S.DRAFT, S.CLOSED_UNVERIFIED, KERNEL)  # only a person closes
    with pytest.raises(InvalidTransitionError):  # archived is legacy: nothing enters it (M3)
        transition(tree.packets[1], S.ARCHIVED, user, "dropped")
    # with evidence the packet finishes sealed (M3a, R6 OQ13)
    complete_with_evidence(session, tree.packets[2], user)
    assert tree.packets[2].status is S.SEALED


def test_terminal_packets_are_immutable(session: Session, tree) -> None:
    packet = tree.packets[0]
    transition(packet, S.CLOSED_UNVERIFIED, USER, "not needed")
    with pytest.raises(ImmutableRecordError):
        transition(packet, S.QUEUED, USER)
    with pytest.raises(ImmutableRecordError):
        transition(packet, S.ARCHIVED, USER)


def test_a_running_packet_never_fails_it_is_requeued(session: Session, tree) -> None:
    """M3a (R6 transitions): running → failed is removed; a crash requeues (running → queued)."""
    packet = tree.packets[0]
    for status in [S.EVALUATING_PRE, S.READY, S.QUEUED, S.RUNNING]:
        transition(packet, status, KERNEL)
    with pytest.raises(InvalidTransitionError, match="not in the packet table"):
        transition(packet, S.FAILED, KERNEL)
    transition(packet, S.QUEUED, KERNEL)
    assert packet.status is S.QUEUED and not packet.locked
