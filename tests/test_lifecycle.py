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

S = PacketStatus
HAPPY_PATH = [
    S.QUEUED,
    S.EVALUATING,
    S.READY,
    S.IN_PROGRESS,
    S.AWAITING_CONFIRMATION,
    S.PROCESSED,
    S.COMPLETED,
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
    assert packet.locked and packet.completed_at is not None


def test_illegal_transition_raises_and_writes_nothing(session: Session, tree) -> None:
    packet = tree.packets[0]
    count = session.scalar(select(func.count()).select_from(Event))
    with pytest.raises(InvalidTransitionError, match="draft → in_progress"):
        transition(packet, S.IN_PROGRESS, KERNEL)
    with pytest.raises(InvalidTransitionError):
        transition(packet, S.COMPLETED, KERNEL)  # the kernel cannot skip the pipeline
    assert session.scalar(select(func.count()).select_from(Event)) == count
    assert packet.status is S.DRAFT


def test_user_may_complete_or_archive_by_hand(session: Session, tree) -> None:
    assert can_transition(S.DRAFT, S.COMPLETED, Actor.user("cli"))
    assert not can_transition(S.DRAFT, S.COMPLETED, KERNEL)
    transition(tree.packets[0], S.COMPLETED, Actor.user("cli"), "done by hand")
    transition(tree.packets[1], S.ARCHIVED, KERNEL, "dropped")
    assert tree.packets[0].locked and tree.packets[1].locked


def test_terminal_packets_are_immutable(session: Session, tree) -> None:
    packet = tree.packets[0]
    transition(packet, S.COMPLETED, USER)
    with pytest.raises(ImmutableRecordError):
        transition(packet, S.QUEUED, USER)
    with pytest.raises(ImmutableRecordError):
        transition(packet, S.ARCHIVED, USER)


def test_failed_packets_can_retry(session: Session, tree) -> None:
    packet = tree.packets[0]
    for status in [S.QUEUED, S.EVALUATING, S.FAILED, S.QUEUED]:
        transition(packet, status, KERNEL)
    assert packet.status is S.QUEUED and not packet.locked
