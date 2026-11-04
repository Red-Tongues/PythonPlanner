"""Typed actors (R1 fix 11): authority follows the actor kind; an LLM never gains user authority."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestrator.actors import Actor, ActorKind
from orchestrator.contracts import PacketStatus
from orchestrator.errors import InvalidTransitionError
from orchestrator.events import record_event
from orchestrator.lifecycle import transition
from tests.conftest import KERNEL, USER
from tests.dbhelpers import (
    LLM,
)

# --------------------------------------------------------------------------- typed actors


def test_actor_is_typed() -> None:
    assert str(Actor.user("cli")) == "user:cli"
    assert Actor.llm("m").kind is ActorKind.LLM
    with pytest.raises(ValueError):
        Actor.user("user:cli spoof")
    with pytest.raises(TypeError):
        Actor("user", "x")  # type: ignore[arg-type]


def test_only_users_complete_or_archive_by_hand(session: Session, tree) -> None:
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


def test_llm_cannot_skip_the_pipeline_from_any_state(session: Session, tree) -> None:
    packet = tree.packets[0]
    for status in (PacketStatus.EVALUATING_PRE, PacketStatus.READY, PacketStatus.QUEUED):
        transition(packet, status, KERNEL)
        with pytest.raises(InvalidTransitionError):
            transition(packet, PacketStatus.COMPLETED, LLM)
